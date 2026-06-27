"""
Agent7 投资领导 — 综合决策引擎

功能：
1. 汇总所有Agent的报告和状态
2. 检查各Agent今日是否已执行
3. 分析各报告一致性（是否存在冲突）
4. 【增强】读取风控官的操盘审查结果（trade_conflicts），自动仲裁风控vs操盘分歧
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
"""

import os
import sys
import json
import re
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))


def load_json(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_report(path: str) -> str:
    if not os.path.exists(path):
        return ""
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def check_agent_status(report_dir: str, today_str: str) -> dict:
    """检查各Agent今日执行状态"""
    agents = {
        "情报员": {"dir": "情报", "prefix": "情报摘要"},
        "分析师": {"dir": "分析", "prefix": "分析报告"},
        "选股机器人": {"dir": "选股", "prefix": "选股建议"},
        "风控官": {"dir": "风控", "prefix": "风控报告"},
        "操盘手": {"dir": "操盘", "prefix": "交易计划"},
    }

    status = {}
    for name, info in agents.items():
        path = os.path.join(report_dir, info["dir"], f"{info['prefix']}_{today_str}.md")
        exists = os.path.exists(path)
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
    if risk_text and trading_text:
        if ("HIGH" in risk_text or "CRITICAL" in risk_text) and "买入" in trading_text:
            conflicts.append({
                "type": "风险-交易冲突",
                "detail": "风控报告为HIGH风险，但操盘手建议买入",
                "severity": "HIGH",
                "suggestion": "以风控为准，取消或缩减买入计划",
            })

    # 选股推荐 vs 风控限制
    stock_text = reports.get("选股", "")
    if stock_text and risk_text:
        if ("震荡" in risk_text or "调整" in risk_text) and "强烈推荐" in stock_text:
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
    raw_dir = os.path.join(os.path.dirname(__file__), "..", "..", "data", "raw")
    pattern = os.path.join(raw_dir, "风控报告_*.json")
    files = sorted(glob.glob(pattern), reverse=True)
    if not files:
        return {}
    try:
        with open(files[0], "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return {}


def load_trade_raw_data() -> dict:
    """读取操盘手最新原始数据（含交易计划明细）"""
    raw_dir = os.path.join(os.path.dirname(__file__), "..", "..", "data", "raw")
    pattern = os.path.join(raw_dir, "交易原始数据_*.json")
    files = sorted(glob.glob(pattern), reverse=True)
    if not files:
        return {}
    try:
        with open(files[0], "r", encoding="utf-8") as f:
            return json.load(f)
    except:
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
    """分析各Agent产出质量（基于关键词检查）"""
    quality = {}
    for name, info in agent_status.items():
        if not info["executed"]:
            quality[name] = {"status": "NOT_EXECUTED", "issues": []}
            continue

        text = reports.get(name, "")
        issues = []

        # 通用的质量检查
        if len(text) < 100:
            issues.append("报告内容过短（<100字）")
        if "风险" not in text and "注意" not in text and name not in ["情报员"]:
            issues.append("缺少风险提示")

        quality[name] = {
            "status": "PASS" if not issues else "NEEDS_REVIEW",
            "issues": issues,
        }

    return quality


def make_decision(reports: dict, agent_status: dict) -> dict:
    """主函数：综合决策"""
    print("[投资领导] 开始综合决策...")
    ok = "[OK]"
    warn = "[WARN]"
    fail = "[FAIL]"

    root = os.path.join(os.path.dirname(__file__), "..", "..")
    today_str = datetime.now().strftime("%Y-%m-%d")
    report_dir = os.path.join(root, "reports", "日报")

    result = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "team_status": {},
        "readiness": {},
        "conflicts": [],
        "agent_quality": {},
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
    report_names = ["情报员", "分析师", "选股机器人", "风控官", "操盘手"]
    report_map = {
        "情报员": ("情报", "情报摘要"),
        "分析师": ("分析", "分析报告"),
        "选股机器人": ("选股", "选股建议"),
        "风控官": ("风控", "风控报告"),
        "操盘手": ("操盘", "交易计划"),
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

    # 6. 质量分析
    quality = analyze_agent_quality(report_contents, agent_status)
    result["agent_quality"] = quality
    for name, q in quality.items():
        if q["status"] == "NEEDS_REVIEW":
            print(f"  {warn} {name} 质量待审核: {'; '.join(q['issues'])}")

    # 7. 市场判断
    risk_text = report_contents.get("风控官", "")
    analysis_text = report_contents.get("分析师", "")

    if "CRITICAL" in risk_text or "极端行情" in risk_text:
        result["market_assessment"] = "极端风险 — 建议空仓"
    elif "HIGH" in risk_text or "熊市" in risk_text:
        result["market_assessment"] = "高风险 — 建议减仓防御"
    elif "WARNING" in risk_text:
        result["market_assessment"] = "中等风险 — 谨慎操作"
    elif "LOW" in risk_text and "牛市" in risk_text:
        result["market_assessment"] = "低风险 — 可积极操作"
    else:
        result["market_assessment"] = "震荡市 — 控制仓位"

    print(f"  {ok} 市场判断: {result['market_assessment']}")

    # 8. 最终决策（含风控vs操盘仲裁）
    has_critical_conflict = any(c["severity"] == "HIGH" for c in conflicts)
    has_arbitration = any(
        a["ruling"] in ("风控一票否决", "暂缓买入，纳入观察", "听从风控，减仓优先")
        for a in arbitrations
    )
    high_risk = "极高" in result["market_assessment"] or "极端" in result["market_assessment"]

    if high_risk:
        result["final_plan"]["should_trade"] = False
        result["final_plan"]["action"] = "不交易 — 极端风险"
        result["veto_notes"].append("市场处于极端/高风控状态，否决所有买入计划")
        print(f"  {warn} 最终决策: 不交易（高风险）")
    elif has_arbitration:
        result["final_plan"]["should_trade"] = False
        result["final_plan"]["action"] = "部分执行 — 否决项纳入观察池"
        result["veto_notes"].append("风控官与操盘手存在分歧，仲裁后否决了部分交易")
        print(f"  {warn} 最终决策: 部分执行（仲裁后否决部分交易）")
        for a in arbitrations:
            result["veto_notes"].append(
                f"{a['name']}({a['code']}): {a['ruling']} — {a['final_action']}"
            )
    elif has_critical_conflict:
        result["final_plan"]["should_trade"] = False
        result["final_plan"]["action"] = "观望 — 存在重大冲突"
        print(f"  {warn} 最终决策: 观望（存在冲突）")
    elif readiness["level"] == "INSUFFICIENT":
        result["final_plan"]["should_trade"] = False
        result["final_plan"]["action"] = "不出新交易 — 信息不足，仅管理持仓止盈止损"
        print(f"  {warn} 最终决策: 仅管理持仓（信息不足）")
    else:
        # 如果仲裁结果是部分止盈（折中方案），仍可交易
        has_partial_execution = any(
            a["ruling"] == "折中：部分止盈" for a in arbitrations
        )
        if has_partial_execution:
            result["final_plan"]["should_trade"] = True
            result["final_plan"]["action"] = "部分止盈 — 其余正常执行"
            print(f"  {ok} 最终决策: 部分止盈+正常执行")
        else:
            result["final_plan"]["should_trade"] = True
            result["final_plan"]["action"] = "可执行交易计划"
            print(f"  {ok} 最终决策: 可执行交易计划")

    # 9. 策略调整建议
    if readiness["level"] == "PARTIAL":
        result["strategy_notes"] = (
            f"今日缺失: {', '.join(readiness['missing_agents'])}。"
            "建议复盘师关注这些Agent未运行对决策质量的影响。"
        )

    return result


if __name__ == "__main__":
    report = make_decision({}, {})

    print("\n=== DECISION ===")
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    print("=== END ===")

    # 保存
    output_dir = os.path.join(os.path.dirname(__file__), "..", "..", "data", "raw")
    os.makedirs(output_dir, exist_ok=True)
    today = datetime.now().strftime("%Y%m%d")
    output_path = os.path.join(output_dir, f"决策原始数据_{today}.json")
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n决策数据已保存: {output_path}")
