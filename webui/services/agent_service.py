"""
Agent 执行服务 — 在后台运行 Agent 脚本，查询执行状态
"""
import os, sys, subprocess, json, time, threading
from datetime import datetime
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
VENV_PYTHON = PROJECT_ROOT / "venv" / "Scripts" / "python.exe"

# ── Agent 脚本映射 ───────────────────────────────────────────────────
AGENTS = {
    "情报": {
        "script": "scripts/agent1-情报采集/fetch_all.py",
        "label": "🕵️ 情报员",
        "desc": "自动抓取全网财经资讯（政策、公告、龙虎榜、资金流向）",
        "timeout": 300,
    },
    "分析": {
        "script": "scripts/agent2-技术分析/analyze.py",
        "label": "📊 分析师",
        "desc": "技术分析 + 板块排名（MACD/KDJ/RSI/布林带）",
        "timeout": 300,
    },
    "选股": {
        "script": "scripts/agent5-选股/stock_picker.py",
        "label": "🔍 选股机器人",
        "desc": "多因子选股（支持 pre_market / intraday / noon / evening 模式）",
        "timeout": 300,
    },
    "风控": {
        "script": "scripts/agent3-风控/risk_check.py",
        "label": "🛡️ 风控官",
        "desc": "风控检查 + 止损监控 + 操盘审查",
        "timeout": 300,
    },
    "操盘": {
        "script": "scripts/agent6-操盘/trader.py",
        "label": "🎯 操盘手",
        "desc": "制定交易计划（接受风控审查）",
        "timeout": 300,
    },
    "决策": {
        "script": "scripts/agent7-决策/leader.py",
        "label": "🏆 投资领导",
        "desc": "综合决策 + 质量审核 + 冲突仲裁",
        "timeout": 300,
    },
    "复盘": {
        "script": "scripts/agent4-复盘/review.py",
        "label": "🔄 复盘师",
        "desc": "复盘 + 偏差分析 + 知识库更新",
        "timeout": 300,
    },
    "政策": {
        "script": "scripts/agent8-政策分析/policy_analyst.py",
        "label": "📜 政策分析师",
        "desc": "政策影响分析 + 宏观解读",
        "timeout": 300,
    },
    "游资": {
        "script": "scripts/agent9-游资追踪/hot_money_tracker.py",
        "label": "🔥 游资追踪师",
        "desc": "龙虎榜分析 + 游资追踪 + 资金情绪",
        "timeout": 300,
    },
}

# ── 运行状态跟踪 ────────────────────────────────────────────────────
_running_jobs: dict = {}
_jobs_lock = threading.Lock()


def get_agent_list() -> list[dict]:
    """获取 Agent 列表及状态"""
    result = []
    for key, cfg in AGENTS.items():
        with _jobs_lock:
            status = _running_jobs.get(key, {}).get("status", "idle")
            started = _running_jobs.get(key, {}).get("started")
        result.append({
            "key": key,
            "label": cfg["label"],
            "desc": cfg["desc"],
            "status": status,
            "started": started,
        })
    return result


def get_agent_status(agent_key: str) -> dict:
    """获取单个 Agent 状态"""
    with _jobs_lock:
        return _running_jobs.get(agent_key, {"status": "idle"})


def run_agent(agent_key: str, extra_args: str = "") -> dict:
    """启动 Agent 脚本（后台线程）"""
    cfg = AGENTS.get(agent_key)
    if not cfg:
        return {"error": f"未知 Agent: {agent_key}"}

    with _jobs_lock:
        if _running_jobs.get(agent_key, {}).get("status") == "running":
            return {"error": f"{cfg['label']} 正在运行中，请等待完成"}

    script_path = PROJECT_ROOT / cfg["script"]
    if not script_path.exists():
        return {"error": f"脚本不存在: {script_path}"}

    cmd = [str(VENV_PYTHON), "-X", "utf8", str(script_path)]
    if extra_args:
        cmd.extend(extra_args.split())

    def _run():
        start_time = datetime.now()
        with _jobs_lock:
            _running_jobs[agent_key] = {
                "status": "running",
                "started": start_time,
                "output": "",
                "exit_code": None,
                "finished": None,
                "pid": None,
            }
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=str(PROJECT_ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            with _jobs_lock:
                _running_jobs[agent_key]["pid"] = proc.pid

            output_lines = []
            for line in iter(proc.stdout.readline, ""):
                output_lines.append(line)
                if len(output_lines) > 200:
                    output_lines = output_lines[-200:]
                with _jobs_lock:
                    _running_jobs[agent_key]["output_last"] = line.strip()

            proc.wait()
            exit_code = proc.returncode
            with _jobs_lock:
                _running_jobs[agent_key].update({
                    "status": "success" if exit_code == 0 else "failed",
                    "exit_code": exit_code,
                    "output": "".join(output_lines),
                    "finished": datetime.now(),
                })
        except Exception as e:
            with _jobs_lock:
                _running_jobs[agent_key].update({
                    "status": "failed",
                    "exit_code": -1,
                    "output": str(e),
                    "finished": datetime.now(),
                })

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    return {"message": f"{cfg['label']} 已启动", "agent_key": agent_key}


def get_agent_output(agent_key: str) -> str:
    """获取 Agent 输出"""
    with _jobs_lock:
        return _running_jobs.get(agent_key, {}).get("output", "")


def run_batch(agent_keys: list[str]) -> list[dict]:
    """批量运行 Agent"""
    results = []
    for key in agent_keys:
        results.append(run_agent(key))
    return results


def run_pipeline() -> dict:
    """运行完整流水线: 情报→政策→游资→分析→选股→风控→操盘→决策→复盘"""
    pipeline_order = ["情报", "政策", "游资", "分析", "选股", "风控", "操盘", "决策", "复盘"]
    started = []
    for key in pipeline_order:
        result = run_agent(key)
        started.append({"key": key, "result": result})
    return {"pipeline": started, "order": pipeline_order}
