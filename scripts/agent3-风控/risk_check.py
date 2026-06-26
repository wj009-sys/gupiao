"""
Agent3 风控官 - 风险管理核心脚本

功能：
1. 读取仓位管理规则和止损规则
2. 读取持仓信息
3. 根据大盘环境评估当前风险等级
4. 检查每笔持仓是否触发止损
5. 检查总仓位和单票仓位是否超限
6. 输出风控报告结构化数据

用法：
    source venv/Scripts/activate
    python -X utf8 scripts/agent3-风控/risk_check.py [--portfolio data/portfolio.json] [--env-score 70]

如果不传环境评分，则从 data/raw/分析原始数据_*.json 中自动读取
"""

import os
import sys
import json
import glob
from datetime import datetime

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from scripts.utils.tushare_client import pro


def load_json(path: str) -> dict:
    """安全加载 JSON 文件"""
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_portfolio(path: str = None) -> dict:
    """加载持仓数据"""
    if path is None:
        path = os.path.join(os.path.dirname(__file__), "..", "..", "data", "portfolio.json")
    return load_json(path)


def load_stop_loss_rules() -> list:
    """加载止损规则"""
    path = os.path.join(os.path.dirname(__file__), "..", "..", "data", "止损规则.json")
    rules = load_json(path)
    return rules.get("规则", [])


def load_position_rules() -> dict:
    """加载仓位管理规则"""
    path = os.path.join(os.path.dirname(__file__), "..", "..", "data", "仓位管理规则.json")
    return load_json(path)


def load_env_score_from_analysis() -> int:
    """从 Agent2 的分析结果中读取环境评分"""
    raw_dir = os.path.join(os.path.dirname(__file__), "..", "..", "data", "raw")
    pattern = os.path.join(raw_dir, "分析原始数据_*.json")
    files = sorted(glob.glob(pattern), reverse=True)
    if not files:
        return None
    latest = files[0]
    try:
        data = load_json(latest)
        return data.get("environment_score")
    except:
        return None


def fetch_stock_price(ts_code: str) -> dict:
    """获取个股最新的实时/日线行情"""
    try:
        df = pro.daily(ts_code=ts_code, start_date="", end_date="")
        if not df.empty:
            last = df.iloc[0]
            return {
                "price": float(last["close"]),
                "pct_chg": float(last.get("pct_chg", 0)),
                "trade_date": last.get("trade_date", ""),
                "high": float(last.get("high", last["close"])),
                "low": float(last.get("low", last["close"])),
            }
    except:
        pass
    return None


def determine_market_environment(env_score: int, index_data: dict = None) -> dict:
    """根据环境评分判断市场状态"""
    if env_score is None and index_data is None:
        return {"level": "unknown", "name": "未知", "max_position": 80, "max_single": 20}

    if env_score is not None:
        if env_score >= 85:
            return {"level": "bull", "name": "牛市确认", "max_position": 100, "max_single": 30}
        elif env_score >= 60:
            return {"level": "range", "name": "震荡市", "max_position": 80, "max_single": 20}
        elif env_score >= 30:
            return {"level": "bear", "name": "熊市/调整", "max_position": 50, "max_single": 10}
        else:
            return {"level": "extreme", "name": "极端行情", "max_position": 0, "max_single": 0}

    # 如果有指数数据但无评分，用均线判断
    if index_data:
        above_ma20 = index_data.get("above_ma20", False)
        above_ma60 = index_data.get("above_ma60", False)
        ma_align = index_data.get("ma_align", "")

        if above_ma60 and "多头" in ma_align:
            return {"level": "bull", "name": "牛市确认", "max_position": 100, "max_single": 30}
        elif above_ma20:
            return {"level": "range", "name": "震荡市", "max_position": 80, "max_single": 20}
        elif above_ma20 is False and above_ma60 is True:
            return {"level": "bear", "name": "熊市/调整", "max_position": 50, "max_single": 10}
        else:
            return {"level": "extreme", "name": "极端行情", "max_position": 0, "max_single": 0}

    return {"level": "range", "name": "震荡市", "max_position": 80, "max_single": 20}


def check_stop_loss(holding: dict, current_price: float, pct_chg: float) -> list:
    """对单个持仓检查是否触发止损"""
    alerts = []
    cost = holding.get("成本价", 0)
    name = holding.get("名称", "未知")
    code = holding.get("代码", "")

    if cost == 0:
        return alerts

    # 当前盈亏比例
    pnl_pct = (current_price - cost) / cost * 100
    holding["当前盈亏%"] = round(pnl_pct, 2)

    # 1. 固定比例止损（-7%）
    rules = load_stop_loss_rules()
    for rule in rules:
        rule_type = rule.get("类型", "")
        params = rule.get("参数", {})

        if rule_type == "固定比例止损":
            threshold = params.get("比例", -7)
            if pnl_pct <= threshold:
                alerts.append({
                    "level": "CRITICAL",
                    "type": "固定止损",
                    "asset": f"{name}({code})",
                    "message": f"亏损 {pnl_pct:.1f}%，触发 {threshold}% 止损线！建议立即执行止损",
                    "pnl_pct": round(pnl_pct, 2),
                    "threshold": threshold,
                    "action": "强制止损",
                })

        elif rule_type == "移动止损":
            # 需要知道最高价=取 hold 期间的最高价
            # 简化：用最新行情的最高价
            retreat = params.get("回撤比例", -5)
            pct_chg_from_high = pct_chg  # 简化
            # 如果盈利状态下回撤超过阈值
            if pnl_pct > 0 and pct_chg <= retreat:
                alerts.append({
                    "level": "WARNING",
                    "type": "移动止损",
                    "asset": f"{name}({code})",
                    "message": f"从高点回撤 {abs(pct_chg):.1f}%，触发移动止损线",
                    "pnl_pct": round(pnl_pct, 2),
                    "threshold": retreat,
                    "action": "止盈/减仓",
                })

    return alerts


def check_position_limits(portfolio: dict, market_env: dict) -> list:
    """检查仓位是否超限"""
    alerts = []
    holdings = portfolio.get("持仓列表", [])
    total_asset = portfolio.get("总资产", 0) or sum(h.get("市值", 0) for h in holdings)

    if total_asset == 0:
        return alerts

    max_position = market_env.get("max_position", 80)
    max_single = market_env.get("max_single", 20)
    market_name = market_env.get("name", "未知")

    # 计算实际总仓位
    total_market_value = sum(h.get("市值", 0) or h.get("持股数量", 0) * h.get("当前价", 0) for h in holdings)
    actual_position_pct = (total_market_value / total_asset) * 100 if total_asset > 0 else 0

    # 总仓位检查
    if actual_position_pct > max_position:
        excess = actual_position_pct - max_position
        alerts.append({
            "level": "WARNING" if excess < 20 else "CRITICAL",
            "type": "总仓位超限",
            "asset": "全部持仓",
            "message": f"当前总仓位 {actual_position_pct:.0f}%，超过 {market_name} 上限 {max_position}%，超限 {excess:.0f}%",
            "actual": round(actual_position_pct, 1),
            "limit": max_position,
            "action": "减仓",
        })

    # 单票仓位检查
    for h in holdings:
        market_value = h.get("市值", 0) or h.get("持股数量", 0) * h.get("当前价", 0)
        single_pct = (market_value / total_asset) * 100 if total_asset > 0 else 0
        name = h.get("名称", "未知")
        code = h.get("代码", "")

        if single_pct > max_single:
            alerts.append({
                "level": "WARNING",
                "type": "单票超限",
                "asset": f"{name}({code})",
                "message": f"{name} 仓位 {single_pct:.0f}%，超过单票上限 {max_single}%",
                "actual": round(single_pct, 1),
                "limit": max_single,
                "action": "减仓",
            })

    return alerts


def check_market_risk(env_score: int, index_analysis: list = None) -> list:
    """大盘环境风险检查"""
    alerts = []

    env = determine_market_environment(env_score, None)

    if env["level"] == "bull":
        alerts.append({
            "level": "INFO",
            "type": "大盘环境",
            "asset": "大盘",
            "message": f"环境评分 {env_score}，牛市环境，可积极操作",
            "action": "正常交易",
        })
    elif env["level"] == "range":
        alerts.append({
            "level": "INFO",
            "type": "大盘环境",
            "asset": "大盘",
            "message": f"环境评分 {env_score}，震荡市，控制仓位在 {env['max_position']}% 以内",
            "action": "谨慎交易",
        })
    elif env["level"] == "bear":
        alerts.append({
            "level": "WARNING",
            "type": "大盘环境",
            "asset": "大盘",
            "message": f"环境评分 {env_score}，市场偏弱，建议仓位降至 {env['max_position']}% 以下",
            "action": "减仓防御",
        })
    elif env["level"] == "extreme":
        alerts.append({
            "level": "CRITICAL",
            "type": "大盘环境",
            "asset": "大盘",
            "message": f"环境评分 {env_score}，极端行情！建议空仓观望",
            "action": "清仓",
        })
    else:
        alerts.append({
            "level": "INFO",
            "type": "大盘环境",
            "asset": "大盘",
            "message": "无法获取环境评分，使用默认震荡市规则",
            "action": "谨慎",
        })

    # 检查大盘联动止损（来自止损规则）
    if index_analysis:
        for idx in index_analysis:
            name = idx.get("name", "")
            above_ma20 = idx.get("above_ma20")
            above_ma60 = idx.get("above_ma60")

            if above_ma20 is False and above_ma60 is True:
                alerts.append({
                    "level": "WARNING",
                    "type": "大盘联动止损",
                    "asset": name,
                    "message": f"{name} 跌破 MA20，建议减仓至 5 成以下",
                    "action": "减仓至5成",
                })
            elif above_ma20 is False and above_ma60 is False:
                alerts.append({
                    "level": "CRITICAL",
                    "type": "大盘联动止损",
                    "asset": name,
                    "message": f"{name} 跌破 MA60！系统性风险信号，建议清仓",
                    "action": "清仓",
                })

    return alerts


def generate_risk_report(portfolio_path: str = None, env_score: int = None) -> dict:
    """主函数：生成完整风控报告"""
    print("[风控官] 开始风险评估...")
    _ok = "[OK]"
    _warn = "[WARN]"
    _fail = "[FAIL]"

    report = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "environment": {"score": env_score, "assessment": "未知", "max_position": 80, "max_single": 20},
        "portfolio_summary": {"total_asset": 0, "total_position": 0, "holding_count": 0},
        "alerts": [],
        "risk_level": "LOW",
        "suggested_action": "无操作",
        "errors": [],
    }

    # 1. 加载配置
    portfolio = load_portfolio(portfolio_path)
    holdings = portfolio.get("持仓列表", [])
    report["portfolio_summary"]["total_asset"] = portfolio.get("总资产", 0)
    report["portfolio_summary"]["holding_count"] = len([h for h in holdings if h.get("代码") != "000000"])

    market_rules = load_position_rules()

    # 2. 如果没有传入环境评分，尝试从分析报告中读取
    if env_score is None:
        env_score = load_env_score_from_analysis()
        print(f"  {_ok} 从分析报告读取环境评分: {env_score}")

    # 3. 确定市场环境
    market_env = determine_market_environment(env_score)
    report["environment"] = {
        "score": env_score,
        "assessment": market_env["name"],
        "max_position": market_env["max_position"],
        "max_single": market_env["max_single"],
    }
    print(f"  {_ok} 市场环境: {market_env['name']} (上限: {market_env['max_position']}%)")

    # 4. 大盘环境检查
    try:
        env_alerts = check_market_risk(env_score)
        report["alerts"].extend(env_alerts)
        for a in env_alerts:
            print(f"  [{a['level']}] {a['type']}: {a['message']}")
    except Exception as e:
        report["errors"].append(f"大盘环境检查失败: {e}")
        print(f"  {_fail} 大盘环境检查: {e}")

    # 5. 检查每个持仓
    for holding in holdings:
        code = holding.get("代码", "")
        name = holding.get("名称", "")
        if code == "000000" or not code:
            continue

        try:
            # 获取最新价格
            price_data = fetch_stock_price(code)
            if price_data:
                current_price = price_data["price"]
                pct_chg = price_data["pct_chg"]
                holding["当前价"] = current_price

                # 止损检查
                stop_loss_alerts = check_stop_loss(holding, current_price, pct_chg)
                report["alerts"].extend(stop_loss_alerts)
                for a in stop_loss_alerts:
                    print(f"  [{a['level']}] {a['type']} {a['asset']}: {a['message']}")
            else:
                print(f"  {_warn} {name}({code}) 无法获取行情")
        except Exception as e:
            report["errors"].append(f"{name}({code}) 检查失败: {e}")
            print(f"  {_fail} {name}: {e}")

    # 6. 仓位检查
    try:
        pos_alerts = check_position_limits(portfolio, market_env)
        report["alerts"].extend(pos_alerts)
        for a in pos_alerts:
            print(f"  [{a['level']}] {a['type']}: {a['message']}")
    except Exception as e:
        report["errors"].append(f"仓位检查失败: {e}")
        print(f"  {_fail} 仓位检查: {e}")

    # 7. 综合风险等级
    critical_count = sum(1 for a in report["alerts"] if a["level"] == "CRITICAL")
    warning_count = sum(1 for a in report["alerts"] if a["level"] == "WARNING")

    if critical_count > 0:
        report["risk_level"] = "HIGH"
        report["suggested_action"] = "立即执行风控措施"
    elif warning_count > 0:
        report["risk_level"] = "MEDIUM"
        report["suggested_action"] = "关注预警项，准备应对"
    else:
        report["risk_level"] = "LOW"
        report["suggested_action"] = "维持当前操作"

    return report


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="风控检查")
    parser.add_argument("--portfolio", default=None, help="持仓JSON路径")
    parser.add_argument("--env-score", type=int, default=None, help="大盘环境评分(0-100)")
    args = parser.parse_args()

    report = generate_risk_report(args.portfolio, args.env_score)

    print("\n=== RISK_REPORT ===")
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    print("=== END ===")

    # 保存报告
    output_dir = os.path.join(os.path.dirname(__file__), "..", "..", "data", "raw")
    os.makedirs(output_dir, exist_ok=True)
    today = datetime.now().strftime("%Y%m%d")
    output_path = os.path.join(output_dir, f"风控报告_{today}.json")
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n风控报告已保存: {output_path}")
