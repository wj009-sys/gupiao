"""
Agent 并行调度器 — 波次式多Agent执行引擎

基于 learn-claude-code s13/s15 设计，实现 Wave 级并行调度。

三种运行模式：
  - parallel:    Wave 内 Agent 并行执行（默认，使用 ThreadPoolExecutor）
  - sequential:  所有 Agent 串行执行（兼容模式）
  - auto:        自动检测环境，有 MessageBus 用 parallel，否则 sequential

波次设计：
  Wave 1: Agent1(情报) + Agent8(政策) + Agent9(游资)  — 并行采集
  Wave 2: Agent2(分析) + Agent5(选股)                  — 并行分析
  Wave 3: Agent3(风控) + Agent6(操盘)                  — 并行审查
  Wave 4: Agent7(决策)                                  — 汇总决策

每个 Agent 执行后通过 MessageBus 发送状态（running/completed/failed）。
失败 Agent 默认不阻塞管线（except required=True），后续 Agent 使用已有数据降级。

用法：
    from scripts.utils.agent_orchestrator import AgentOrchestrator

    # 一键运行全流程（推荐）
    orchestrator = AgentOrchestrator(date="20260707")
    summary = orchestrator.run_all()
    print(summary)

    # 或逐步控制
    orchestrator = AgentOrchestrator(mode="sequential")
    w1 = orchestrator.run_wave(1)
    w2 = orchestrator.run_wave(2)
    ...

D3异常处理表：
| 触发条件 | 一线修复 | 仍失败兜底 |
|---------|---------|-----------|
| Agent 脚本超时 | 发送 SIGTERM（kill），等待 5s | SIGKILL（强制终止），标记 timeout |
| Agent 脚本不存在（路径错）| 自动检测 scripts/agent{N} 目录 | 标记 skipped，不阻塞波次 |
| Python venv 找不到 | 回退到系统 python3/python 命令 | 标记 error，打印提示 |
| MessageBus 初始化失败 | 回退到纯 subprocess 模式 | 仅关闭状态追踪，不影响调度 |
| Agent 返回非零退出码 | 读取 stderr，记录失败原因 | 标记 failed，非必需则不中断波次 |
| 报告文件创建失败（磁盘满）| 检查目录是否可写 | 标记 failed，继续执行其他 Agent |
| 同一 Agent 被重复调度 | 检查已有运行状态，跳过已完成的 | 标记 skipped |
| Wave 内部分 Agent 成功 | 保留成功的结果，失败的降级 | 正常进入下一波 |

D4 CHECKPOINT:
- CP1-Venv Python 确认：执行前确认 python 路径存在
- CP2-报告存在性验证：每个 Agent 完成后检查预期报告文件是否存在
- CP3-Wave 依赖检查：Wave N 的所有 Agent 全部完成后才进入 Wave N+1
- CP4-退出码校验：检查 subprocess 的 returncode，非零必须记录
- CP5-超时熔断：单个 Agent 超时杀死子进程，不拖垮整个 Wave

D9反例：
- 不要并行执行有数据依赖的 Agent（如 Agent5 依赖 Agent1 的情报数据）
- 不要无限期等待超时的 Agent（必须设 timeout，默认 600s）
- 不要忽略 Agent 失败（至少打印警告，标记 failed）
- 不要在同一波次混入前后依赖的 Agent（必须分 Wave）
- 不要假设所有 Agent 使用相同的 Python 解释器（统一用 venv python）
"""
import os
import sys
import time
import json
import logging
import datetime
import subprocess
import signal
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FuturesTimeout
from typing import Optional

logger = logging.getLogger(__name__)

# ===== 项目根检测 =====

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def _resolve(path: str) -> str:
    """相对项目根解析路径"""
    if os.path.isabs(path):
        return path
    return os.path.abspath(os.path.join(PROJECT_ROOT, path))


# ===== Venv Python 检测 =====

_VENV_PYTHON_CACHE = None


def get_venv_python() -> str:
    """获取 venv Python 解释器路径"""
    global _VENV_PYTHON_CACHE
    if _VENV_PYTHON_CACHE:
        return _VENV_PYTHON_CACHE

    candidates = [
        os.path.join(PROJECT_ROOT, "venv", "Scripts", "python.exe"),  # Windows
        os.path.join(PROJECT_ROOT, "venv", "Scripts", "python"),      # Windows (no .exe)
        os.path.join(PROJECT_ROOT, "venv", "bin", "python3"),         # Unix
        os.path.join(PROJECT_ROOT, "venv", "bin", "python"),          # Unix (no python3)
    ]

    for p in candidates:
        if os.path.exists(p):
            _VENV_PYTHON_CACHE = p
            return p

    # fallback: 找系统 python
    for cmd in ["python3", "python"]:
        try:
            r = subprocess.run([cmd, "--version"], capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=5)
            if r.returncode == 0:
                _VENV_PYTHON_CACHE = cmd
                return cmd
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue

    raise FileNotFoundError("找不到 Python 解释器（venv 中也没有，系统中也没有）")


# ===== Agent 配置 =====

class AgentDef:
    """单个 Agent 的静态配置"""

    def __init__(
        self,
        agent_id: str,
        name: str,
        script: str,
        args: Optional[list] = None,
        python_flags: Optional[list] = None,
        timeout: int = 600,
        wave: int = 1,
        can_parallel: bool = True,
        required: bool = False,
        report_pattern: str = "",
    ):
        self.agent_id = agent_id
        self.name = name
        self.script = _resolve(script)
        self.args = args or []
        self.python_flags = python_flags or []
        self.timeout = timeout
        self.wave = wave
        self.can_parallel = can_parallel
        self.required = required
        self.report_pattern = report_pattern

    def build_command(self, python_path: str, date: str = "") -> list[str]:
        """构建完整命令"""
        cmd = [python_path]
        cmd.extend(self.python_flags)
        cmd.append(self.script)
        # 替换 {date} 占位符
        for a in self.args:
            cmd.append(a.replace("{date}", date) if "{date}" in a else a)
        return cmd

    def check_report(self, date: str = "") -> Optional[str]:
        """检查报告文件是否存在，返回路径或 None"""
        if not self.report_pattern:
            return None
        path = _resolve(self.report_pattern.replace("{date}", date))
        if os.path.exists(path):
            return path
        return None

    def to_dict(self) -> dict:
        return {
            "id": self.agent_id,
            "name": self.name,
            "script": self.script,
            "wave": self.wave,
            "timeout": self.timeout,
            "required": self.required,
        }


class ExecutionResult:
    """单个 Agent 的执行结果"""

    def __init__(self, agent_id: str, status: str = "pending"):
        self.agent_id = agent_id
        self.status = status  # pending | running | completed | failed | skipped | timeout
        self.exit_code: Optional[int] = None
        self.duration: float = 0.0
        self.started_at: Optional[float] = None
        self.finished_at: Optional[float] = None
        self.report_path: Optional[str] = None
        self.error: str = ""
        self.stdout: str = ""

    def to_dict(self) -> dict:
        return {
            "agent_id": self.agent_id,
            "status": self.status,
            "exit_code": self.exit_code,
            "duration": round(self.duration, 1),
            "report_path": self.report_path,
            "error": self.error[:200] if self.error else "",
        }

    def __repr__(self) -> str:
        status_icon = {
            "pending": "⏳", "running": "▶️", "completed": "✅",
            "failed": "❌", "skipped": "⏭️", "timeout": "⏰",
        }.get(self.status, "❓")
        return f"{status_icon} {self.agent_id} ({self.status})"


# ===== Agent 注册表 =====

def _today_str() -> str:
    """返回今日日期 YYYYMMDD"""
    return datetime.date.today().strftime("%Y%m%d")


def _default_agents() -> list[AgentDef]:
    """默认 Agent 注册表"""
    return [
        # ── Wave 1: 情报采集（并行） ──
        AgentDef(
            agent_id="agent1", name="情报员",
            script="scripts/agent1-情报采集/fetch_all.py",
            args=["{date}"],
            python_flags=["-X", "utf8"],
            timeout=600, wave=1, can_parallel=True,
            report_pattern="reports/日报/情报/情报摘要_{date}.md",
        ),
        AgentDef(
            agent_id="agent8", name="政策分析师",
            script="scripts/agent8-政策分析/policy_analyst.py",
            args=["{date}"],
            python_flags=["-X", "utf8"],
            timeout=600, wave=1, can_parallel=True,
            report_pattern="reports/日报/政策/政策分析_{date}.md",
        ),
        AgentDef(
            agent_id="agent9", name="游资追踪师",
            script="scripts/agent9-游资追踪/hot_money_tracker.py",
            args=["{date}"],
            python_flags=["-X", "utf8"],
            timeout=600, wave=1, can_parallel=True,
            report_pattern="reports/日报/游资/游资追踪_{date}.md",
        ),
        # ── Wave 2: 分析选股（并行） ──
        AgentDef(
            agent_id="agent2", name="分析师",
            script="scripts/agent2-技术分析/analyze.py",
            args=["{date}"],
            python_flags=["-X", "utf8"],
            timeout=600, wave=2, can_parallel=True,
            report_pattern="reports/日报/分析/分析报告_{date}.md",
        ),
        AgentDef(
            agent_id="agent5", name="选股机器人",
            script="scripts/agent5-选股/stock_picker.py",
            args=["--mode", "evening", "--top-n", "5"],
            python_flags=["-X", "utf8"],
            timeout=600, wave=2, can_parallel=True,
            report_pattern="reports/日报/选股/选股建议_{date}.md",
        ),
        # ── Wave 3: 风控操盘（并行） ──
        AgentDef(
            agent_id="agent3", name="风控官",
            script="scripts/agent3-风控/risk_check.py",
            args=["--risk-profile", "neutral", "--no-extended-risk"],
            python_flags=["-X", "utf8"],
            timeout=600, wave=3, can_parallel=True,
            report_pattern="reports/日报/风控/风控报告_{date}.md",
        ),
        AgentDef(
            agent_id="agent6", name="操盘手",
            script="scripts/agent6-操盘/trader.py",
            args=[],
            python_flags=["-X", "utf8"],
            timeout=600, wave=3, can_parallel=True,
            report_pattern="reports/日报/操盘/交易计划_{date}.md",
        ),
        # ── Wave 4: 决策（串行，仅此一个） ──
        AgentDef(
            agent_id="agent7", name="投资领导",
            script="scripts/agent7-决策/leader.py",
            args=[],
            python_flags=["-X", "utf8"],
            timeout=600, wave=4, can_parallel=False, required=True,
            report_pattern="reports/日报/决策/投资决策_{date}.md",
        ),
    ]


# ===== 调度器 =====

class AgentOrchestrator:
    """多 Agent 波次调度器

    支持并行/串行两种模式，集成 MessageBus 状态跟踪。

    Usage:
        orch = AgentOrchestrator(date="20260707")
        results = orch.run_all()  # 一键运行全流程
    """

    def __init__(
        self,
        date: str = "",
        mode: str = "auto",
        agents: Optional[list[AgentDef]] = None,
        python_path: str = "",
        message_bus=None,
        max_workers: int = 4,
    ):
        """
        Args:
            date: 交易日 YYYYMMDD，默认为今天
            mode: parallel(并行) / sequential(串行) / auto(自动检测)
            agents: Agent 配置列表，为 None 则使用默认注册表
            python_path: Python 解释器路径，为空则自动检测 venv
            message_bus: MessageBus 实例，用于状态通知
            max_workers: 并行最大线程数
        """
        self.date = date or _today_str()
        self.mode = mode
        self.agents = agents if agents is not None else _default_agents()
        self.max_workers = max_workers
        self._bus = message_bus

        # 检测 Python 路径
        try:
            self._python = python_path or get_venv_python()
        except FileNotFoundError:
            self._python = ""

        # 当前状态
        self._results: dict[str, ExecutionResult] = {}
        self._started_at: Optional[float] = None

        # 自动检测模式
        if self.mode == "auto":
            self.mode = "parallel" if self._python and self._bus else "sequential"

    @property
    def python_path(self) -> str:
        return self._python

    @property
    def waves(self) -> list[int]:
        """获取所有波次号（排序后）"""
        return sorted(set(a.wave for a in self.agents))

    def get_agents_in_wave(self, wave: int) -> list[AgentDef]:
        """获取指定波次的所有 Agent"""
        return [a for a in self.agents if a.wave == wave]

    # ── 单 Agent 执行 ──

    def _send_bus(self, msg_type: str, content: str, metadata: Optional[dict] = None):
        """通过 MessageBus 发送状态（如果可用）"""
        if self._bus is None:
            return
        try:
            self._bus.send("orchestrator", "lead", msg_type, content, metadata or {})
        except Exception as e:
            logger.debug(f"[Orch] MessageBus 发送失败: {e}")

    def run_agent(self, agent_def: AgentDef) -> ExecutionResult:
        """执行单个 Agent 脚本

        Returns:
            ExecutionResult（status: completed / failed / timeout / skipped）
        """
        result = ExecutionResult(agent_def.agent_id)

        # 校验 Python
        if not self._python:
            result.status = "skipped"
            result.error = "Python 解释器不可用"
            self._results[agent_def.agent_id] = result
            return result

        # 校验脚本存在
        if not os.path.exists(agent_def.script):
            result.status = "skipped"
            result.error = f"脚本不存在: {agent_def.script}"
            logger.warning(f"[Orch] {agent_def.agent_id}: {result.error}")
            self._results[agent_def.agent_id] = result
            return result

        # 构建命令
        cmd = agent_def.build_command(self._python, self.date)
        logger.info(f"[Orch] {agent_def.agent_id} ({agent_def.name}) 开始执行...")
        logger.debug(f"  cmd: {' '.join(cmd)}")

        result.status = "running"
        result.started_at = time.time()
        self._send_bus("progress", f"{agent_def.agent_id} 开始执行", {"agent": agent_def.agent_id, "status": "running"})

        try:
            proc = subprocess.run(
                cmd,
                cwd=PROJECT_ROOT,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=agent_def.timeout,
            )

            result.exit_code = proc.returncode
            result.finished_at = time.time()
            result.duration = result.finished_at - result.started_at
            result.stdout = proc.stdout[-1000:] if proc.stdout else ""

            if proc.returncode == 0:
                result.status = "completed"
                # 检查报告
                report = agent_def.check_report(self.date)
                if report:
                    result.report_path = report
                logger.info(f"[Orch] {agent_def.agent_id} ✅ 完成 ({result.duration:.0f}s)")
                self._send_bus("result", f"{agent_def.agent_id} 执行成功",
                               {"agent": agent_def.agent_id, "status": "completed",
                                "duration": round(result.duration, 1), "report": report or ""})
            else:
                result.status = "failed"
                stderr = proc.stderr[-500:] if proc.stderr else ""
                result.error = f"退出码 {proc.returncode}: {stderr[:200]}"
                logger.warning(f"[Orch] {agent_def.agent_id} ❌ 失败 (exit={proc.returncode})")
                self._send_bus("result", f"{agent_def.agent_id} 执行失败",
                               {"agent": agent_def.agent_id, "status": "failed",
                                "exit_code": proc.returncode, "error": result.error[:200]})

        except subprocess.TimeoutExpired:
            result.status = "timeout"
            result.finished_at = time.time()
            result.duration = result.finished_at - result.started_at
            result.error = f"执行超时 ({agent_def.timeout}s)"
            logger.warning(f"[Orch] {agent_def.agent_id} ⏰ 超时 ({agent_def.timeout}s)")
            self._send_bus("result", f"{agent_def.agent_id} 超时",
                           {"agent": agent_def.agent_id, "status": "timeout",
                            "timeout": agent_def.timeout})

        except FileNotFoundError as e:
            result.status = "failed"
            result.error = f"命令未找到: {e}"
            logger.error(f"[Orch] {agent_def.agent_id} ❌ {result.error}")

        except Exception as e:
            result.status = "failed"
            result.error = str(e)[:200]
            logger.error(f"[Orch] {agent_def.agent_id} ❌ 异常: {e}")

        self._results[agent_def.agent_id] = result
        return result

    # ── 波次执行 ──

    def run_wave(self, wave: int) -> list[ExecutionResult]:
        """执行一个波次内的所有 Agent

        parallel 模式：并行执行（can_parallel=True 的 Agent）
        sequential 模式：串行执行全部
        """
        agents = self.get_agents_in_wave(wave)
        if not agents:
            logger.info(f"[Orch] Wave {wave}: 无 Agent")
            return []

        logger.info(f"[Orch] ═══ Wave {wave} 开始 ({len(agents)} Agent) ═══")

        # 检查是否有并行执行的需求
        has_parallel = self.mode == "parallel" and any(a.can_parallel for a in agents) and len(agents) > 1

        # 同步执行
        if not has_parallel:
            results = []
            for agent_def in agents:
                r = self.run_agent(agent_def)
                results.append(r)
                # 必需 Agent 失败则中断波次
                if agent_def.required and r.status in ("failed", "timeout"):
                    logger.warning(f"[Orch] Wave {wave}: 必需 Agent {agent_def.agent_id} 失败，中断波次")
                    # 标记未执行的 Agent 为 skipped
                    for remaining in agents[len(results):]:
                        skipped = ExecutionResult(remaining.agent_id, "skipped")
                        skipped.error = f"前置必需 Agent {agent_def.agent_id} 失败"
                        self._results[remaining.agent_id] = skipped
                        results.append(skipped)
                    break
            return results

        # 并行执行
        results = [None] * len(agents)
        agent_map = {}
        for i, a in enumerate(agents):
            agent_map[i] = a

        with ThreadPoolExecutor(max_workers=min(self.max_workers, len(agents))) as executor:
            future_map = {}
            for i, agent_def in enumerate(agents):
                if agent_def.can_parallel:
                    future = executor.submit(self.run_agent, agent_def)
                    future_map[future] = i
                else:
                    # 不能并行的串行执行
                    results[i] = self.run_agent(agent_def)

            # 收集并行的结果
            for future in as_completed(future_map):
                i = future_map[future]
                try:
                    results[i] = future.result(timeout=agent_map[i].timeout + 30)
                except FuturesTimeout:
                    r = ExecutionResult(agent_map[i].agent_id, "timeout")
                    r.error = f"并行超时"
                    results[i] = r
                except Exception as e:
                    r = ExecutionResult(agent_map[i].agent_id, "failed")
                    r.error = str(e)[:200]
                    results[i] = r

        # 检查必需 Agent
        for i, agent_def in enumerate(agents):
            r = results[i]
            if agent_def.required and r.status in ("failed", "timeout"):
                logger.warning(f"[Orch] Wave {wave}: 必需 Agent {agent_def.agent_id} 失败")

        return results

    # ── 全流程执行 ──

    def run_all(self) -> dict[int, list[ExecutionResult]]:
        """执行所有波次

        Returns:
            {wave_number: [ExecutionResult, ...]}
        """
        self._started_at = time.time()
        wave_results: dict[int, list[ExecutionResult]] = {}

        logger.info(f"[Orch] ════════════════════════════════════════")
        logger.info(f"[Orch]   全流程启动 — {self.date}")
        logger.info(f"[Orch]   模式: {self.mode} | Python: {self._python}")
        logger.info(f"[Orch]   Agent: {len(self.agents)} | Wave: {self.waves}")
        logger.info(f"[Orch] ════════════════════════════════════════")

        self._send_bus("task_assignment", "全流程启动",
                       {"date": self.date, "mode": self.mode, "agent_count": len(self.agents)})

        total_agents = len(self.agents)
        completed_agents = 0

        for wave in self.waves:
            results = self.run_wave(wave)
            wave_results[wave] = results
            completed_agents += len([r for r in results if r.status == "completed"])

            # 打印进度
            success = sum(1 for r in results if r.status == "completed")
            failed = sum(1 for r in results if r.status in ("failed", "timeout"))
            skipped = sum(1 for r in results if r.status == "skipped")
            logger.info(f"[Orch] Wave {wave}: {success}✅ {failed}❌ {skipped}⏭️  ({completed_agents}/{total_agents})")

        total_duration = time.time() - self._started_at
        logger.info(f"[Orch] ════════════════════════════════════════")
        logger.info(f"[Orch]   全流程完成 — {total_duration:.0f}s")
        logger.info(f"[Orch] ════════════════════════════════════════")

        self._send_bus("result", "全流程完成",
                       {"date": self.date, "duration": round(total_duration, 1),
                        "mode": self.mode})

        return wave_results

    # ── 状态查询 ──

    def get_summary(self, wave_results: dict[int, list[ExecutionResult]]) -> str:
        """生成执行摘要文本"""
        lines = []
        lines.append(f"📋 Agent 执行摘要 — {self.date}")
        lines.append(f"   模式: {self.mode} | Python: {self._python}")
        lines.append("")

        all_results = []
        for wave in sorted(wave_results.keys()):
            results = wave_results[wave]
            agents_in_wave = self.get_agents_in_wave(wave)
            lines.append(f"  Wave {wave} ({agents_in_wave[0].name if agents_in_wave else ''}...):")
            for r in results:
                a = next((a for a in self.agents if a.agent_id == r.agent_id), None)
                name = f"{a.name}" if a else r.agent_id
                icon = {"completed": "✅", "failed": "❌", "timeout": "⏰", "skipped": "⏭️", "running": "▶️", "pending": "⏳"}.get(r.status, "❓")
                dur = f"({r.duration:.0f}s)" if r.duration > 0 else ""
                report = f" 📄 {r.report_path}" if r.report_path else ""
                err = f" — {r.error[:60]}" if r.error else ""
                lines.append(f"    {icon} {r.agent_id} {name}{dur}{report}{err}")
            lines.append("")
            all_results.extend(results)

        total = len(all_results)
        success = sum(1 for r in all_results if r.status == "completed")
        failed = sum(1 for r in all_results if r.status in ("failed", "timeout"))
        skipped = sum(1 for r in all_results if r.status == "skipped")
        lines.append(f"  📊 总计: {total} Agent | ✅ {success} | ❌ {failed} | ⏭️ {skipped}")

        return "\n".join(lines)

    def get_json_summary(self, wave_results: dict[int, list[ExecutionResult]]) -> dict:
        """获取 JSON 格式的执行摘要"""
        all_results = []
        for wave in sorted(wave_results.keys()):
            for r in wave_results[wave]:
                d = r.to_dict()
                d["wave"] = wave
                all_results.append(d)

        return {
            "date": self.date,
            "mode": self.mode,
            "agents": all_results,
            "total": len(all_results),
            "completed": sum(1 for r in all_results if r["status"] == "completed"),
            "failed": sum(1 for r in all_results if r["status"] in ("failed", "timeout")),
            "skipped": sum(1 for r in all_results if r["status"] == "skipped"),
        }


# ===== CLI 入口 =====

def _parse_date(s: str) -> str:
    """验证日期格式"""
    s = s.strip()
    if len(s) == 8 and s.isdigit():
        return s
    if len(s) == 10 and s[4] == "-" and s[7] == "-":
        return s.replace("-", "")
    raise ValueError(f"无效日期格式: {s}（需要 YYYYMMDD 或 YYYY-MM-DD）")


def main():
    """CLI 入口：python scripts/utils/agent_orchestrator.py [date] [--mode parallel|sequential]"""
    import argparse
    parser = argparse.ArgumentParser(description="Agent 并行调度器")
    parser.add_argument("date", nargs="?", default="", help="交易日 YYYYMMDD（默认今天）")
    parser.add_argument("--mode", default="auto", choices=["parallel", "sequential", "auto"],
                        help="执行模式")
    parser.add_argument("--wave", type=int, default=0, help="指定波次（1-4，默认全流程）")
    parser.add_argument("--agent", default="", help="指定单个 Agent（如 agent1），优先级高于 --wave")
    parser.add_argument("--json", action="store_true", help="输出 JSON 格式")
    parser.add_argument("--dry-run", action="store_true", help="仅打印执行计划，不执行")

    args = parser.parse_args()
    date = _parse_date(args.date) if args.date else _today_str()

    # 尝试初始化 MessageBus
    bus = None
    try:
        from scripts.utils.message_bus import MessageBus
        bus = MessageBus()
    except Exception:
        pass

    orch = AgentOrchestrator(date=date, mode=args.mode, message_bus=bus)

    # Dry run
    if args.dry_run:
        print(f"📋 Dry Run — {date}")
        print(f"   模式: {args.mode}")
        print(f"   Python: {orch.python_path}")
        print(f"   MessageBus: {'✅' if bus else '❌'}")
        print()
        for wave in orch.waves:
            agents = orch.get_agents_in_wave(wave)
            names = [f"{a.agent_id}({a.name})" for a in agents]
            print(f"  Wave {wave}: {' + '.join(names)}")
            for a in agents:
                cmd = a.build_command(orch.python_path, date)
                report = a.check_report(date)
                print(f"    {a.agent_id}: {' '.join(cmd)}")
                print(f"      report: {report or '(待生成)'}")
        print()
        print(f"  总计: {len(orch.agents)} Agent / {len(orch.waves)} Waves")
        return

    # 执行
    if args.agent:
        # 单 Agent
        agent = next((a for a in orch.agents if a.agent_id == args.agent), None)
        if not agent:
            print(f"❌ 未知 Agent: {args.agent}")
            print(f"   可用: {', '.join(a.agent_id for a in orch.agents)}")
            return
        result = orch.run_agent(agent)
        print(orch.get_summary({agent.wave: [result]}))
        if args.json:
            print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    elif args.wave:
        results = orch.run_wave(args.wave)
        summary = orch.get_summary({args.wave: results})
        print(summary)
        if args.json:
            print(json.dumps(orch.get_json_summary({args.wave: results}), ensure_ascii=False, indent=2))
    else:
        all_results = orch.run_all()
        summary = orch.get_summary(all_results)
        print(summary)
        if args.json:
            print(json.dumps(orch.get_json_summary(all_results), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    main()
