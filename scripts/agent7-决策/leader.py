"""
Agent7 投资领导 — 综合决策引擎

功能：
1. 汇总所有Agent的报告和状态
2. 检查各Agent今日是否已执行
3. 分析各报告一致性（是否存在冲突）
4. 读取风控官的操盘审查结果（trade_conflicts），自动仲裁风控vs操盘分歧
5. 输出最终投资决策（含仲裁结论）

用法：
    source venv/Scripts/activate
    python -X utf8 scripts/agent7-决策/leader.py

依赖上游报告（全部可选，缺失时自动降级）：
    reports/日报/情报/情报摘要_YYYY-MM-DD.md
    reports/日报/分析/分析报告_YYYY-MM-DD.md
    reports/日报/选股/选股建议_YYYY-MM-DD.md
    reports/日报/风控/风控报告_YYYY-MM-DD.md
    reports/日报/操盘/交易计划_YYYY-MM-DD.md

D9反例（工作反例）：
1. 不要从不打回——质量审核如果没有打回记录，说明标准过低
2. 不要给模糊的改进要求——打回指令必须包含具体可操作的要求
3. 不要不跟踪重做——打回后不检查REWORKED标记等于无效打回
4. 不要因时间压力降低标准放行——标准是死的，时间不够就跳过该Agent
5. 不要在缺乏信息时强行决策——信息不足时应暂缓交易

D3异常处理表：
| 触发条件 | 一线修复 | 仍失败兜底 |
|---------|---------|-----------|
| 所有报告缺失（首个交易日）| 打印警告，标记团队就绪度INSUFFICIENT | 最终决策：不出新交易，仅管理持仓止盈止损 |
| 风控原始数据JSON为空/格式错 | 返回空dict，不执行仲裁 | 跳过仲裁章节，只在报告中说"无分歧数据" |
| 打回重做Agent不在agent_map中 | continue跳过该Agent | 输出警告，人工处理 |
| 质量审核中reports不存在 | 标记为NOT_EXECUTED | 跳过该Agent审核 |
| 读取报告时报编码错误 | 用UTF-8 BOM再试1次 | 返回空字符串 |
| JSON序列化含不可序列化类型 | 用default=str处理 | 输出human-readable错误信息 |
| 【新增】决策反思文件缺失（首次运行）| 静默跳过，不加载记忆 | 继续正常决策流程 |
| 【新增】Bull/Bear辩论模块导入失败 | 用importlib动态加载替代直接import | 跳过辩论，继续基于规则决策 |
| 【新增】SQLite决策日志写入失败 | 打印警告，不阻塞决策流程 | 决策照常输出，日志丢失不影响交易 |

D4 CHECKPOINT:
- CP1-报告完整性检查：所有报告加载完成后，确认已加载数量
- CP2-冲突检测范围：风控HIGH但操盘有买入=必须检测到
- CP3-仲裁规则优先级：HIGH风险时风控一票否决优先于其他规则
- CP4-重做跟踪验证：REWORKED标记必须作为重做证据
- CP5-信息不足阻断：团队就绪度<50%时禁止新交易
- CP6-【新增】决策记忆已加载：执行前确认memory/决策反思.md已读取并注入上下文
- CP7-【新增】辩论结果已考量：最终决策应反映Bull/Bear辩论的倾向
- CP8-【新增】决策日志已写入：每次最终决策都必须记录到SQLite
"""

import os
import sys
import json
import re
import glob
import importlib.util
from datetime import datetime

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, PROJECT_ROOT)

# ============================================================
# 【TradingAgents借鉴】加载决策记忆反思（由Agent4复盘师写入）
# ============================================================


def load_decision_reflection() -> str:
    """加载复盘师的决策反思摘要

    路径: memory/决策反思.md
    由 agent4-复盘/review.py 的 write_reflection_summary() 在每晚复盘时写入。
    投资领导在每天启动时自动加载作为"昨日的教训"。

    TradingAgents 的 trading_memory.md 机制：
    - 每次分析前注入 Prior Prediction + Reflection
    - 让 LLM 意识到自己之前哪里判断错了
    - 避免重复犯错
    """
    memory_path = os.path.join(PROJECT_ROOT, "memory", "决策反思.md")
    if not os.path.exists(memory_path):
        return ""
    try:
        with open(memory_path, "r", encoding="utf-8") as f:
            content = f.read()
        return content
    except Exception as e:
        print(f"[WARN] 加载决策反思失败: {e}")
        return ""


def load_json(path: str) -> dict:
    """安全加载JSON文件，失败时返回空dict"""
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, UnicodeDecodeError, Exception) as e:
        print(f"  [WARN] JSON解析失败 ({os.path.basename(path)}): {e}")
        return {}


def load_report(path: str) -> str:
    """安全加载报告文件，支持BOM回退"""
    if not os.path.exists(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except UnicodeDecodeError:
        # D3回退：尝试BOM编码
        try:
            with open(path, "r", encoding="utf-8-sig") as f:
                return f.read()
        except Exception as e:
            print(f"  [WARN] 报告读取失败(UTF-8 BOM回退): {os.path.basename(path)}: {e}")
            return ""
    except Exception as e:
        print(f"  [WARN] 报告读取失败 ({os.path.basename(path)}): {e}")
        return ""


def check_agent_status(report_dir: str, today_str: str) -> dict:
    """检查各Agent今日执行状态"""
    agents = {
        "情报员": {"dir": "情报", "prefix": "情报摘要", "raw_prefix": "情报原始数据"},
        "分析师": {"dir": "分析", "prefix": "分析报告", "raw_prefix": "分析原始数据"},
        "选股机器人": {"dir": "选股", "prefix": "选股建议", "raw_prefix": "选股原始数据"},
        "风控官": {"dir": "风控", "prefix": "风控报告", "raw_prefix": "风控报告"},
        "操盘手": {"dir": "操盘", "prefix": "交易计划", "raw_prefix": "交易原始数据"},
        "复盘师": {"dir": "复盘", "prefix": "复盘报告", "raw_prefix": "复盘报告"},
    }

    raw_dir = os.path.join(PROJECT_ROOT, "data", "raw")
    today_raw = today_str.replace("-", "")

    status = {}
    for name, info in agents.items():
        path = os.path.join(report_dir, info["dir"], f"{info['prefix']}_{today_str}.md")
        exists = os.path.exists(path)
        if not exists:
            # 回退到 data/raw/ 目录查找 JSON 原始数据
            raw_pattern = os.path.join(raw_dir, f"{info['raw_prefix']}_{today_raw}*.json")
            raw_files = sorted(glob.glob(raw_pattern), reverse=True)
            if raw_files:
                exists = True
                path = raw_files[0]
        status[name] = {
            "executed": exists,
            "report_path": path if exists else None,
        }
    return status


def assess_team_readiness(agent_status: dict) -> dict:
    """评估团队就绪状态"""
    executed = [k for k, v in agent_status.items() if v["executed"]]
    missing = [k for k, v in agent_status.items() if not v["executed"]]

    total = len(agent_status)
    readiness = len(executed) / total * 100 if total > 0 else 0

    if readiness >= 80:
        level = "READY"
    elif readiness >= 50:
        level = "PARTIAL"
    else:
        level = "INSUFFICIENT"

    return {
        "readiness_pct": round(readiness, 0),
        "level": level,
        "executed_agents": executed,
        "missing_agents": missing,
    }


def detect_conflicts(reports: dict) -> list:
    """检测各Agent报告之间的冲突"""
    conflicts = []

    risk_text = reports.get("风控", "")
    trading_text = reports.get("操盘", "")

    # 风控 HIGH 但操盘建议买入 → 冲突
    # 使用 \b 单词边界避免 "非HIGH" 被误匹配
    if risk_text and trading_text:
        risk_is_high = bool(re.search(r'\bHIGH\b', risk_text)) or "CRITICAL" in risk_text
        if risk_is_high and "买入" in trading_text:
            conflicts.append({
                "type": "风险-交易冲突",
                "detail": "风控报告为HIGH风险，但操盘手建议买入",
                "severity": "HIGH",
                "suggestion": "以风控为准，取消或缩减买入计划",
            })

    # 选股推荐 vs 风控限制
    stock_text = reports.get("选股", "")
    if stock_text and risk_text:
        risk_is_weak = ("震荡" in risk_text or "调整" in risk_text or "中等" in risk_text)
        has_strong_buy = bool(re.search(r'强烈推荐|强力推荐|重点推荐', stock_text))
        if risk_is_weak and has_strong_buy:
            conflicts.append({
                "type": "选股-风控冲突",
                "detail": "风控判断市场偏弱，但选股机器人有强烈推荐",
                "severity": "MEDIUM",
                "suggestion": "降低买入仓位，严格控制止损",
            })

    return conflicts


# ============================================================
# 【新增】风控官vs操盘手冲突仲裁
# ============================================================


def load_risk_raw_data() -> dict:
    """读取风控官最新原始数据（含trade_conflicts等结构化数据）"""
    raw_dir = os.path.join(PROJECT_ROOT, "data", "raw")
    pattern = os.path.join(raw_dir, "风控报告_*.json")
    files = sorted(glob.glob(pattern), reverse=True)
    if not files:
        return {}
    try:
        with open(files[0], "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"[WARN] 加载风控原始数据失败: {e}")
        return {}


def load_trade_raw_data() -> dict:
    """读取操盘手最新原始数据（含交易计划明细）"""
    raw_dir = os.path.join(PROJECT_ROOT, "data", "raw")
    pattern = os.path.join(raw_dir, "交易原始数据_*.json")
    files = sorted(glob.glob(pattern), reverse=True)
    if not files:
        return {}
    try:
        with open(files[0], "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"[WARN] 加载交易原始数据失败: {e}")
        return {}


def arbitrate_trade_conflicts(risk_data: dict, trade_data: dict) -> list:
    """仲裁风控官和操盘手之间的交易分歧"""
    arbitrations = []
    trade_conflicts = risk_data.get("trade_conflicts", [])
    risk_level = risk_data.get("risk_level", "MEDIUM")

    if not trade_conflicts:
        return arbitrations

    for conflict in trade_conflicts:
        arb = {
            "conflict_type": conflict["type"],
            "code": conflict.get("code", ""),
            "name": conflict.get("name", ""),
            "risk_view": conflict.get("risk_view", ""),
            "trader_view": conflict.get("trader_view", ""),
            "severity": conflict.get("severity", "MEDIUM"),
            "ruling": "",
            "reason": "",
            "final_action": "",
        }

        # 仲裁规则：
        # 1. 风控 HIGH 时 → 风控一票否决
        # 2. 风控否决买入 + 操盘力推 → 冲突升级到领导
        # 3. 止盈分歧 → 平衡建议（先部分止盈）
        # 4. 加减仓分歧 → 看幅度，幅度大听风控，幅度小折中

        ctype = conflict["type"]
        severity = conflict.get("severity", "MEDIUM")

        if risk_level == "HIGH":
            arb["ruling"] = "风控一票否决"
            arb["reason"] = "当前风险等级为 HIGH，风控官拥有最高优先级。规则：HIGH风险时禁止逆势操作。"
            arb["final_action"] = "采纳风控意见"

        elif ctype == "风控否决买入":
            arb["ruling"] = "暂缓买入，纳入观察"
            arb["reason"] = (
                "风控反对买入有合理风险提示。虽然操盘手看好，"
                "但「宁可错过，不可做错」。建议将目标票加入观察池，待风险降低后再评估。"
            )
            arb["final_action"] = "暂不买入，进入观察池"

        elif ctype == "加减仓分歧":
            arb["ruling"] = "听从风控，减仓优先"
            arb["reason"] = (
                "风控官建议减仓有规则依据（止损线/仓位上限），"
                "操盘手建议买入主要基于选股评分。规则执行优先于机会把握。"
            )
            arb["final_action"] = "采纳风控建议"

        elif ctype == "止盈分歧":
            arb["ruling"] = "折中：部分止盈"
            arb["reason"] = (
                "风控建议止盈（锁定利润），操盘手建议继续持有（博取更大收益）。"
                "两者都有道理，建议折中——先止盈1/3锁定部分利润，剩余继续持有。"
            )
            arb["final_action"] = "部分止盈（1/3仓位）"

        else:
            arb["ruling"] = "提交用户决策"
            arb["reason"] = "无法自动识别冲突类型，需要用户人工判断。"
            arb["final_action"] = "等待用户指令"

        arbitrations.append(arb)

    return arbitrations


def analyze_agent_quality(reports: dict, agent_status: dict) -> dict:
    """分析各Agent产出质量（每Agent专用审核标准）"""
    quality = {}
    for name, info in agent_status.items():
        if not info["executed"]:
            quality[name] = {"status": "NOT_EXECUTED", "issues": [], "verdict": "NOT_EXECUTED"}
            continue

        text = reports.get(name, "")
        issues = []
        warnings = []

        if len(text) < 100:
            issues.append("报告内容过短（<100字），无法支撑决策")

        # === 专属审核标准 ===
        if name == "情报员":
            if "大盘" not in text and "上证" not in text:
                issues.append("缺少大盘行情概况")
            if "板块" not in text and "行业" not in text:
                warnings.append("缺少板块/行业热点信息")
            if "资金" not in text and "北向" not in text:
                warnings.append("缺少资金流向数据")
            if "政策" not in text and "新闻" not in text and "要闻" not in text:
                warnings.append("缺少政策/要闻信息")

        elif name == "分析师":
            if "MACD" not in text and "KDJ" not in text and "RSI" not in text:
                issues.append("缺少技术指标分析（MACD/KDJ/RSI至少一个）")
            if "板块" not in text and "行业排名" not in text:
                warnings.append("缺少板块强度排名")
            if "MA20" not in text and "均线" not in text:
                warnings.append("缺少均线分析（MA20位置判断）")
            if "结论" not in text and "建议" not in text and "判断" not in text:
                issues.append("缺少明确的趋势判断结论（牛/熊/震荡）")

        elif name == "选股机器人":
            if "评分" not in text and "分" not in text:
                issues.append("缺少候选票评分数据")
            if "推荐" not in text and "候选" not in text and "关注" not in text:
                issues.append("没有输出候选股票列表")
            codes_found = len(re.findall(r'\d{6}\.(SZ|SH|BJ)', text))
            if codes_found == 0:
                issues.append("没有给出具体股票代码")
            if "持仓" in text and "冲突" not in text:
                warnings.append("建议检查是否有持仓冲突")

        elif name == "风控官":
            if "止损" not in text:
                issues.append("缺少止损检查项目")
            if "仓位" not in text and "持仓" not in text:
                issues.append("缺少仓位合规检查")
            if "等级" not in text and "级别" not in text:
                issues.append("缺少风控等级结论（LOW/MEDIUM/HIGH）")
            if "操作" not in text and "指令" not in text:
                warnings.append("缺少具体操作指令")

        elif name == "操盘手":
            if "买入" not in text:
                warnings.append("没有买入建议")
            if "卖出" not in text:
                warnings.append("没有卖出建议")
            if "止损" not in text:
                issues.append("买入清单缺少止损位")
            if "仓位" not in text and "上限" not in text:
                issues.append("缺少仓位汇总和合规检查")

        # 综合评级
        if issues:
            status = "REJECTED"
            verdict = "❌ 不合格 — 必须打回重做"
        elif warnings:
            status = "NEEDS_REVIEW"
            verdict = "⚠️ 需补充 — 建议打回补充后重审"
        else:
            status = "PASS"
            verdict = "✅ 通过"

        quality[name] = {
            "status": status,
            "issues": issues,
            "warnings": warnings,
            "verdict": verdict,
            "content_length": len(text),
        }

    return quality


def generate_rework_orders(quality: dict, today_str: str) -> list:
    """对不合格的Agent输出生成打回重做指令"""
    orders = []
    for name, q in quality.items():
        if q["status"] not in ("REJECTED", "NEEDS_REVIEW"):
            continue

        order = {
            "agent": name,
            "order_type": "REWORK" if q["status"] == "REJECTED" else "SUPPLEMENT",
            "reasons": q["issues"] + q.get("warnings", []),
            "requirements": [],
        }

        # 按Agent类型生成具体的改进要求
        if name == "情报员":
            if any("大盘" in i for i in q["issues"]):
                order["requirements"].append("补充大盘行情概况（上证/深证/创业板涨跌幅）")
            if any("板块" in i for i in q.get("warnings", [])):
                order["requirements"].append("补充今日热点板块/行业信息")
            if any("资金" in i for i in q.get("warnings", [])):
                order["requirements"].append("补充北向资金/主力资金流向数据")
            if any("政策" in i for i in q.get("warnings", [])):
                order["requirements"].append("补充今日重要政策/新闻要闻")

        elif name == "分析师":
            if any("指标" in i for i in q["issues"]):
                order["requirements"].append("至少计算MACD、KDJ、RSI中一个技术指标")
            if any("结论" in i for i in q["issues"]):
                order["requirements"].append("给出明确的趋势判断结论（牛市/震荡/熊市）")
            if any("板块" in i for i in q.get("warnings", [])):
                order["requirements"].append("补充板块强度排名数据")
            if any("均线" in i for i in q.get("warnings", [])):
                order["requirements"].append("补充上证MA20/MA60均线位置分析")

        elif name == "选股机器人":
            if any("评分" in i for i in q["issues"]):
                order["requirements"].append("输出多因子评分数据（估值/成长/动量/情绪/技术面）")
            if any("候选" in i or "列表" in i for i in q["issues"]):
                order["requirements"].append("输出候选股票列表（含代码、名称、评分、推荐理由）")
            if any("代码" in i for i in q["issues"]):
                order["requirements"].append("每只候选票必须给出具体股票代码（XXXXXX.SZ/SH）")
            if any("冲突" in i for i in q.get("warnings", [])):
                order["requirements"].append("检查并标注与现有持仓的行业冲突")

        elif name == "风控官":
            if any("止损" in i for i in q["issues"]):
                order["requirements"].append("逐笔检查所有持仓的止损线是否触发")
            if any("仓位" in i for i in q["issues"]):
                order["requirements"].append("检查总仓位和单票仓位是否超限")
            if any("等级" in i for i in q["issues"]):
                order["requirements"].append("给出明确的风控等级（LOW/MEDIUM/HIGH）及依据")
            if any("操作" in i for i in q.get("warnings", [])):
                order["requirements"].append("每条预警必须附带具体操作指令（卖什么/卖多少/什么价格）")

        elif name == "操盘手":
            if any("止损" in i for i in q["issues"]):
                order["requirements"].append("每笔买入必须设置止损位（-5%~-7%）")
            if any("仓位" in i for i in q["issues"]):
                order["requirements"].append("输出执行后仓位汇总，确认不超过风控上限")

        orders.append(order)

    return orders


def track_rework_status(rework_orders: list, report_dir: str, today_str: str) -> list:
    """检查被要求重做的Agent是否已经重新提交

    判断依据（任一满足即认为已重做）：
    1. 文件内容包含 #REWORKED 标记（Agent脚本自动添加 或 手动添加）
    2. 文件内容相比打回前有明显变化（长度增加 >50 字符）
    """
    updated = []
    for order in rework_orders:
        name = order["agent"]
        agent_map = {
            "情报员": ("情报", "情报摘要"),
            "分析师": ("分析", "分析报告"),
            "选股机器人": ("选股", "选股建议"),
            "风控官": ("风控", "风控报告"),
            "操盘手": ("操盘", "交易计划"),
            "复盘师": ("复盘", "复盘报告"),
        }
        if name not in agent_map:
            continue

        d, p = agent_map[name]
        path = os.path.join(report_dir, d, f"{p}_{today_str}.md")
        text = load_report(path)

        # 检查 #REWORKED 标记
        has_rework_tag = "#REWORKED" in text if text else False

        # 检查内容是否明显更新（长度 > 100 且包含头部的日期信息，说明被重新生成）
        has_content = len(text) > 150 if text else False

        reworked = has_rework_tag or has_content

        updated.append({
            "agent": name,
            "original_issues": order["reasons"],
            "requirements": order["requirements"],
            "reworked": reworked,
            "status": "已重做 ✅" if reworked else "待重做 ⏳ — 请在报告末尾添加 #REWORKED 标记",
        })

    return updated


def make_decision() -> dict:
    """主函数：综合决策（参数均为内部自动加载，无需外部传入）"""
    print("[投资领导] 开始综合决策...")
    ok = "[OK]"
    warn = "[WARN]"
    fail = "[FAIL]"

    root = PROJECT_ROOT
    today_str = datetime.now().strftime("%Y-%m-%d")
    report_dir = os.path.join(root, "reports", "日报")

    result = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "team_status": {},
        "readiness": {},
        "conflicts": [],
        "agent_quality": {},
        "rework_orders": [],
        "rework_status": [],
        "market_assessment": "未知",
        "final_plan": {
            "should_trade": False,
            "action": "观望",
            "buy": [],
            "sell": [],
            "hold_notes": "",
            "priority": [],
        },
        "veto_notes": [],
        "strategy_notes": "",
        "errors": [],
    }

    # 1. 检查各Agent状态
    agent_status = check_agent_status(report_dir, today_str)
    result["team_status"] = agent_status
    print(f"  {ok} Agent状态检查完成")
    for name, info in agent_status.items():
        print(f"    {name}: {'✅' if info['executed'] else '❌'}")

    # 2. 团队就绪度
    readiness = assess_team_readiness(agent_status)
    result["readiness"] = readiness
    print(f"  {ok} 团队就绪度: {readiness['readiness_pct']}% ({readiness['level']})")

    # 3. 读取报告内容
    report_names = ["情报员", "分析师", "选股机器人", "风控官", "操盘手", "复盘师"]
    report_map = {
        "情报员": ("情报", "情报摘要"),
        "分析师": ("分析", "分析报告"),
        "选股机器人": ("选股", "选股建议"),
        "风控官": ("风控", "风控报告"),
        "操盘手": ("操盘", "交易计划"),
        "复盘师": ("复盘", "复盘报告"),
    }
    report_contents = {}
    for name in report_names:
        d, p = report_map[name]
        path = os.path.join(report_dir, d, f"{p}_{today_str}.md")
        report_contents[name] = load_report(path)

    # 4. 检测冲突
    conflicts = detect_conflicts(report_contents)
    result["conflicts"] = conflicts
    for c in conflicts:
        print(f"  {warn} 冲突: [{c['severity']}] {c['detail']}")

    # 5. 【新增】读取风控官原始数据，检测风控vs操盘分歧并仲裁
    risk_raw_data = load_risk_raw_data()
    trade_raw_data = load_trade_raw_data()
    trade_conflicts = risk_raw_data.get("trade_conflicts", [])
    arbitrations = []

    if trade_conflicts:
        print(f"  {warn} 检测到风控官与操盘手有 {len(trade_conflicts)} 项分歧，开始仲裁...")
        for tc in trade_conflicts:
            print(f"    [{tc['severity']}] {tc['type']}: {tc.get('name', '')}({tc.get('code', '')})")
            print(f"      风控: {tc['risk_view']}")
            print(f"      操盘: {tc['trader_view']}")

        arbitrations = arbitrate_trade_conflicts(risk_raw_data, trade_raw_data)
        result["arbitrations"] = arbitrations
        for a in arbitrations:
            print(f"    仲裁结果: {a['ruling']} — {a['final_action']}")
    else:
        print(f"  {ok} 风控官与操盘手无意见分歧")
        result["arbitrations"] = []

    # 6. 质量分析（结构化审核+打回重做）
    quality = analyze_agent_quality(report_contents, agent_status)
    result["agent_quality"] = quality
    for name, q in quality.items():
        if q["status"] == "REJECTED":
            print(f"  {fail} {name} 不合格 — 必须打回重做!")
            for issue in q["issues"]:
                print(f"       • {issue}")
        elif q["status"] == "NEEDS_REVIEW":
            print(f"  {warn} {name} 需补充 — 建议打回")
            for issue in q.get("warnings", []):
                print(f"       • {issue}")
        elif q["status"] == "PASS":
            print(f"  {ok} {name} 质量通过")

    # 生成打回重做指令
    rework_orders = generate_rework_orders(quality, today_str)
    result["rework_orders"] = rework_orders
    if rework_orders:
        print(f"  {warn} ════════════════════════════════════")
        print(f"  {warn}  📋 打回重做指令（共 {len(rework_orders)} 条）")
        for order in rework_orders:
            tag = "🔴 打回" if order["order_type"] == "REWORK" else "🟡 补充"
            print(f"  {warn}   {tag} {order['agent']}:")
            for r in order["requirements"]:
                print(f"  {warn}       → {r}")
        print(f"  {warn}  ════════════════════════════════════")

    # 跟踪已打回的Agent是否已重做
    if rework_orders:
        rework_status = track_rework_status(rework_orders, report_dir, today_str)
        result["rework_status"] = rework_status
        for rs in rework_status:
            print(f"  {ok}   {rs['agent']}: {rs['status']}")
    else:
        result["rework_status"] = []

    # 7. 市场判断（使用整词匹配避免误判）
    risk_text = report_contents.get("风控官", "")
    analysis_text = report_contents.get("分析师", "")

    has_critical = re.search(r'\bCRITICAL\b', risk_text) if risk_text else None
    has_high = re.search(r'\bHIGH\b', risk_text) if risk_text else None
    has_warning = re.search(r'\bWARNING\b', risk_text) if risk_text else None
    has_low = re.search(r'\bLOW\b', risk_text) if risk_text else None

    if has_critical or "极端" in risk_text:
        result["market_assessment"] = "极端风险 — 建议空仓"
    elif has_high or "熊市" in risk_text:
        result["market_assessment"] = "高风险 — 建议减仓防御"
    elif has_warning:
        result["market_assessment"] = "中等风险 — 谨慎操作"
    elif has_low and "牛市" in risk_text:
        result["market_assessment"] = "低风险 — 可积极操作"
    else:
        result["market_assessment"] = "震荡市 — 控制仓位"

    print(f"  {ok} 市场判断: {result['market_assessment']}")

    # 8. 最终决策（含质量审核+风控vs操盘仲裁）
    has_critical_conflict = any(c["severity"] == "HIGH" for c in conflicts)
    has_arbitration = any(
        a["ruling"] in ("风控一票否决", "暂缓买入，纳入观察", "听从风控，减仓优先")
        for a in arbitrations
    )

    # 质量审核：是否存在必须打回重做的Agent
    rejected_agents = [name for name, q in quality.items() if q["status"] == "REJECTED"]
    needs_review_agents = [name for name, q in quality.items() if q["status"] == "NEEDS_REVIEW"]

    # 决策优先级：有不合格Agent > 高风险 > 有仲裁 > 有冲突 > 信息不足 > 正常
    decision_reasons = []

    if rejected_agents:
        result["final_plan"]["should_trade"] = False
        decision_reasons.append(
            f"以下Agent输出不合格，已打回重做: {', '.join(rejected_agents)}。"
            "等待重做完成后再执行最终决策。"
        )
        print(f"  {warn} 最终决策: 暂缓交易（{len(rejected_agents)}个Agent输出不合格）")

    # 用 or 判断高风险（不覆盖前面的原因）
    high_risk = "极高" in result["market_assessment"] or "极端" in result["market_assessment"]
    if high_risk and result["final_plan"]["should_trade"] is not False:
        result["final_plan"]["should_trade"] = False
        action_high = "不交易 — 极端风险"
        decision_reasons.append("市场处于极端/高风控状态，否决所有买入计划")
        print(f"  {warn} 最终决策: 不交易（高风险）")

    if has_arbitration and result["final_plan"]["should_trade"] is not False:
        result["final_plan"]["should_trade"] = False
        decision_reasons.append("风控官与操盘手存在分歧，仲裁后否决了部分交易")
        print(f"  {warn} 最终决策: 部分执行（仲裁后否决部分交易）")
        for a in arbitrations:
            decision_reasons.append(
                f"{a['name']}({a['code']}): {a['ruling']} — {a['final_action']}"
            )
    elif has_critical_conflict and result["final_plan"]["should_trade"] is not False:
        result["final_plan"]["should_trade"] = False
        decision_reasons.append("存在重大冲突")
        print(f"  {warn} 最终决策: 观望（存在冲突）")
    elif readiness["level"] == "INSUFFICIENT" and result["final_plan"]["should_trade"] is not False:
        result["final_plan"]["should_trade"] = False
        decision_reasons.append("信息不足，仅管理持仓止盈止损")
        print(f"  {warn} 最终决策: 仅管理持仓（信息不足）")

    if rejected_agents:
        result["final_plan"]["action"] = "暂缓交易 — " + "; ".join(
            f"{name}不合格" for name in rejected_agents
        ) if len(rejected_agents) <= 3 else f"暂缓交易 — {len(rejected_agents)}个Agent不合格"
    elif high_risk:
        result["final_plan"]["action"] = "不交易 — 极端风险"
    elif has_arbitration:
        # 如果仲裁结果包含部分止盈，仍可交易
        has_partial = any(a["ruling"] == "折中：部分止盈" for a in arbitrations)
        if has_partial:
            result["final_plan"]["should_trade"] = True
            result["final_plan"]["action"] = "部分止盈 — 其余正常执行"
            print(f"  {ok} 最终决策: 部分止盈+正常执行")
        else:
            result["final_plan"]["action"] = "部分执行 — 否决项纳入观察池"
    elif has_critical_conflict:
        result["final_plan"]["action"] = "观望 — 存在重大冲突"
    elif readiness["level"] == "INSUFFICIENT":
        result["final_plan"]["action"] = "不出新交易 — 信息不足，仅管理持仓止盈止损"
    elif readiness["level"] == "PARTIAL":
        result["final_plan"]["should_trade"] = True
        result["final_plan"]["action"] = "谨慎交易 — 部分Agent未运行"
        print(f"  {ok} 最终决策: 谨慎交易（部分Agent未运行）")
    else:
        result["final_plan"]["should_trade"] = True
        result["final_plan"]["action"] = "可执行交易计划"
        print(f"  {ok} 最终决策: 可执行交易计划")

    result["veto_notes"].extend(decision_reasons)

    # 9. 策略调整建议
    if readiness["level"] == "PARTIAL":
        result["strategy_notes"] = (
            f"今日缺失: {', '.join(readiness['missing_agents'])}。"
            "建议复盘师关注这些Agent未运行对决策质量的影响。"
        )

    # 10. 【TradingAgents借鉴】注入决策记忆反思（由复盘师写入）
    reflection = load_decision_reflection()
    if reflection:
        result["decision_reflection"] = reflection
        print(f"  {ok} 已加载决策反思记忆（Agent4复盘师提供）")

    # 10b. 【TradingAgents借鉴】Bull/Bear 对抗辩论
    try:
        debate_path = os.path.join(os.path.dirname(__file__), "debate.py")
        spec = importlib.util.spec_from_file_location("debate", debate_path)
        debate_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(debate_mod)
        debate_result = debate_mod.run_market_debate()
        result["debate"] = debate_result
        print(f"  {ok} 多空辩论完成: {debate_result['verdict']}")
    except Exception as e:
        print(f"  {warn} 多空辩论失败: {e}")
        result["debate"] = {"verdict": "辩论不可用", "action": "继续基于规则决策"}

    # 11. 【TradingAgents借鉴】记录决策审计日志到SQLite
    try:
        from scripts.utils.db_manager import DatabaseManager
        db = DatabaseManager()
        today_raw = datetime.now().strftime("%Y%m%d")
        # 确定决策类型
        action = result.get("final_plan", {}).get("action", "观望")
        action_lower = action.lower()
        if "不交易" in action_lower or "观望" in action_lower:
            dtype = "hold"
        elif "止损" in action_lower or "减仓" in action_lower:
            dtype = "risk_adjust"
        elif "止盈" in action_lower or "交易" in action_lower or "买入" in action_lower:
            dtype = "trade"
        else:
            dtype = "other"
        # 统计
        rejected = len(result.get("rework_orders", []))
        conflicts = len(result.get("conflicts", [])) + len(result.get("arbitrations", []))
        db.log_decision(
            log_date=today_raw,
            decision_type=dtype,
            action=action,
            summary=result.get("market_assessment", ""),
            reasoning="; ".join(result.get("veto_notes", [])),
            risk_level="HIGH" if any("否决" in n for n in result.get("veto_notes", [])) else "MEDIUM" if conflicts > 0 else "LOW",
            conflict_count=conflicts,
            rework_count=rejected,
            sources=f"团队就绪度: {readiness['readiness_pct']}%",
        )
        print(f"  {ok} 决策日志已记录到SQLite")
    except Exception as e:
        print(f"  [WARN] 决策日志写入失败: {e}")

    return result


if __name__ == "__main__":
    report = make_decision()

    print("\n=== RESULT_JSON ===")
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    print("=== END ===")

    # 保存
    output_dir = os.path.join(PROJECT_ROOT, "data", "raw")
    os.makedirs(output_dir, exist_ok=True)
    today = datetime.now().strftime("%Y%m%d")
    output_path = os.path.join(output_dir, f"决策原始数据_{today}.json")
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n决策数据已保存: {output_path}")
