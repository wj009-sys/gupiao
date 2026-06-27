"""
Agent7 投资领导 — 综合决策引擎

功能：
1. 汇总所有Agent的报告和状态
2. 检查各Agent今日是否已执行
3. 分析各报告一致性（是否存在冲突）
4. 输出最终投资决策

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

    # 5. 质量分析
    quality = analyze_agent_quality(report_contents, agent_status)
    result["agent_quality"] = quality
    for name, q in quality.items():
        if q["status"] == "NEEDS_REVIEW":
            print(f"  {warn} {name} 质量待审核: {'; '.join(q['issues'])}")

    # 6. 市场判断
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

    # 7. 最终决策
    has_critical_conflict = any(c["severity"] == "HIGH" for c in conflicts)
    high_risk = "极高" in result["market_assessment"] or "极端" in result["market_assessment"]

    if high_risk:
        result["final_plan"]["should_trade"] = False
        result["final_plan"]["action"] = "不交易 — 极端风险"
        result["veto_notes"].append("市场处于极端/高风控状态，否决所有买入计划")
        print(f"  {warn} 最终决策: 不交易（高风险）")
    elif has_critical_conflict:
        result["final_plan"]["should_trade"] = False
        result["final_plan"]["action"] = "观望 — 存在重大冲突"
        print(f"  {warn} 最终决策: 观望（存在冲突）")
    elif readiness["level"] == "INSUFFICIENT":
        result["final_plan"]["should_trade"] = False
        result["final_plan"]["action"] = "不出新交易 — 信息不足，仅管理持仓止盈止损"
        print(f"  {warn} 最终决策: 仅管理持仓（信息不足）")
    else:
        result["final_plan"]["should_trade"] = True
        result["final_plan"]["action"] = "可执行交易计划"
        print(f"  {ok} 最终决策: 可执行交易计划")

    # 8. 策略调整建议
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
