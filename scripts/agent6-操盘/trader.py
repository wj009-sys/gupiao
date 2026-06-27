"""
Agent6 操盘手 — 交易计划生成器

功能：
1. 读取选股建议、风控报告、分析报告
2. 读取持仓、仓位规则、止损规则
3. 生成买入/卖出/持有清单
4. 计算仓位分配
5. 输出交易计划报告

用法：
    source venv/Scripts/activate
    python -X utf8 scripts/agent6-操盘/trader.py

依赖上游报告：
    reports/日报/选股/选股建议_YYYY-MM-DD.md  — 候选池
    reports/日报/风控/风控报告_YYYY-MM-DD.md  — 风险等级
    reports/日报/分析/分析报告_YYYY-MM-DD.md  — 关键价位
"""

import os
import sys
import json
import glob
import re
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from scripts.utils.tushare_client import pro


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


def extract_top_picks(intelligence_text: str) -> list:
    """从选股建议文本中解析被推荐的股票列表（简化实现）"""
    picks = []
    # 匹配股票代码模式（6位数字+市场后缀）
    codes = re.findall(r'\b(\d{6}\.(SZ|SH))\b', intelligence_text)
    names = re.findall(r'\*\*(.+?)\((\d{6}\.(SZ|SH))\)\*\*', intelligence_text)

    for name, code in names:
        picks.append({"name": name, "code": code})

    # 如果上面的模式没匹配到，用代码列表兜底
    if not picks:
        seen = set()
        for code, market in codes:
            if code not in seen:
                seen.add(code)
                picks.append({"name": code, "code": code})

    return picks


def get_stock_real_price(ts_code: str) -> dict:
    """获取个股实时/最新行情"""
    try:
        df = pro.daily(ts_code=ts_code, start_date="", end_date="")
        if df is not None and not df.empty:
            last = df.iloc[0]
            return {
                "price": float(last["close"]),
                "high": float(last.get("high", last["close"])),
                "low": float(last.get("low", last["close"])),
                "pct_chg": float(last.get("pct_chg", 0)),
                "volume": float(last.get("vol", 0)),
                "trade_date": last.get("trade_date", ""),
            }
    except:
        pass
    return None


def calculate_position_size(
    total_asset: float,
    max_position_pct: float,
    max_single_pct: float,
    current_position: float,
    num_new_picks: int,
) -> dict:
    """计算可分配仓位"""
    available = total_asset * (max_position_pct / 100 - current_position / 100)
    per_stock_max = total_asset * (max_single_pct / 100)

    return {
        "total_available": max(0, available),
        "per_stock_max": per_stock_max,
        "suggested_per_stock": min(
            available / max(num_new_picks, 1), per_stock_max
        ),
    }


def fetch_technical_levels(ts_code: str) -> dict:
    """获取技术支撑/压力位"""
    try:
        df = pro.daily(ts_code=ts_code, start_date="", end_date="")
        if df is None or df.empty or len(df) < 20:
            return {"support": None, "resistance": None}

        closes = df["close"].values[:60]
        if len(closes) >= 20:
            ma20 = sum(closes[:20]) / 20
            ma60 = sum(closes[:60]) / 60 if len(closes) >= 60 else None
            return {"support": round(min(closes[:10]), 2), "resistance": round(max(closes[:10]), 2)}
    except:
        pass
    return {"support": None, "resistance": None}


def generate_trade_plan() -> dict:
    """主函数：生成交易计划"""
    print("[操盘手] 开始制定交易计划...")
    ok = "[OK]"
    warn = "[WARN]"
    fail = "[FAIL]"

    root = os.path.join(os.path.dirname(__file__), "..", "..")
    today_str = datetime.now().strftime("%Y-%m-%d")
    date_tag = datetime.now().strftime("%Y%m%d")

    result = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "risk_level": "unknown",
        "market_env": {},
        "portfolio": {},
        "buy_plan": [],
        "sell_plan": [],
        "hold_plan": [],
        "position_summary": {},
        "warnings": [],
        "errors": [],
    }

    # 1. 读取配置和上游报告
    portfolio = load_json(os.path.join(root, "data", "portfolio.json"))
    position_rules = load_json(os.path.join(root, "data", "仓位管理规则.json"))
    stoploss_rules = load_json(os.path.join(root, "data", "止损规则.json"))
    strategy_rules = load_json(os.path.join(root, "data", "策略规则.json"))

    # 读取上游报告（尽可能宽容）
    report_dir = os.path.join(root, "reports", "日报")
    stock_picks_text = load_report(os.path.join(report_dir, "选股", f"选股建议_{today_str}.md"))
    risk_text = load_report(os.path.join(report_dir, "风控", f"风控报告_{today_str}.md"))
    analysis_text = load_report(os.path.join(report_dir, "分析", f"分析报告_{today_str}.md"))

    if not stock_picks_text:
        result["warnings"].append("无选股建议报告，将仅管理现有持仓")
        print(f"  {warn} 未找到选股建议")
    else:
        print(f"  {ok} 已读取选股建议")

    if not risk_text:
        result["warnings"].append("无风控报告，将使用保守模式（总仓位≤50%）")
        print(f"  {warn} 未找到风控报告")
    else:
        print(f"  {ok} 已读取风控报告")

    # 2. 解析风控等级
    risk_level = "MEDIUM"
    if risk_text:
        if "HIGH" in risk_text or "CRITICAL" in risk_text:
            risk_level = "HIGH"
        elif "LOW" in risk_text and "WARNING" not in risk_text:
            risk_level = "LOW"
    result["risk_level"] = risk_level

    # 3. 确定仓位上限
    market_env_name = "震荡市"
    if risk_level == "HIGH":
        max_position = 50
        max_single = 10
    elif risk_level == "LOW":
        max_position = 80
        max_single = 20
    else:
        max_position = 65
        max_single = 15

    # 尝试从风控文本中读取仓位限制
    max_match = re.search(r'上限\D*(\d+)%', risk_text)
    if max_match:
        max_position = min(int(max_match.group(1)), max_position)
    result["market_env"] = {"max_position": max_position, "max_single": max_single}
    print(f"  {ok} 仓位上限: {max_position}%（单票≤{max_single}%）")

    # 4. 持仓分析
    holdings = portfolio.get("持仓列表", [])
    total_asset = portfolio.get("总资产", 1000000)
    total_position_value = sum(
        h.get("市值", 0) or h.get("持股数量", 0) * h.get("当前价", 0)
        for h in holdings if h.get("代码") != "000000"
    )
    current_position_pct = (total_position_value / total_asset * 100) if total_asset > 0 else 0

    result["portfolio"] = {
        "total_asset": total_asset,
        "current_position": round(current_position_pct, 1),
        "holding_count": len([h for h in holdings if h.get("代码") != "000000"]),
    }
    print(f"  {ok} 当前总仓位: {current_position_pct:.1f}%")

    # 5. 分析选股建议中的候选票
    if stock_picks_text:
        picks = extract_top_picks(stock_picks_text)
        print(f"  {ok} 从选股建议中解析出 {len(picks)} 只候选票")

        position_info = calculate_position_size(
            total_asset, max_position, max_single, current_position_pct, len(picks)
        )

        for pick in picks:
            price_data = get_stock_real_price(pick["code"])
            levels = fetch_technical_levels(pick["code"])

            buy_item = {
                "code": pick["code"],
                "name": pick["name"],
                "current_price": price_data["price"] if price_data else 0,
                "pct_chg": price_data["pct_chg"] if price_data else 0,
                "suggested_position_pct": min(
                    round(position_info["suggested_per_stock"] / total_asset * 100, 1),
                    max_single,
                ),
                "support": levels["support"],
                "resistance": levels["resistance"],
                "stop_loss": round(price_data["price"] * 0.93, 2) if price_data else 0,
                "reason": "选股机器人推荐（详见选股建议报告）",
            }
            result["buy_plan"].append(buy_item)
            print(f"  {ok} 建议买入: {pick['name']}({pick['code']}) "
                  f"仓位{buy_item['suggested_position_pct']}% "
                  f"止损{buy_item['stop_loss']}")

    # 6. 现有持仓处理
    for h in holdings:
        code = h.get("代码", "")
        if code == "000000" or not code:
            continue
        name = h.get("名称", "未知")
        price_data = get_stock_real_price(code)
        current_price = price_data["price"] if price_data else h.get("当前价", 0)
        cost = h.get("成本价", 0)
        pnl_pct = ((current_price - cost) / cost * 100) if cost > 0 else 0

        # 检查止损
        stop_loss_triggered = False
        if pnl_pct <= -7:
            stop_loss_triggered = True
            result["sell_plan"].append({
                "code": code,
                "name": name,
                "reason": "触发-7%止损线",
                "pnl_pct": round(pnl_pct, 1),
                "suggested_price": current_price,
                "action": "市价卖出",
                "priority": "高",
            })
            print(f"  ⛔ 止损触发: {name}({code}) 亏损{pnl_pct:.1f}% → 建议卖出")

        # 检查止盈
        elif pnl_pct >= 15:
            result["sell_plan"].append({
                "code": code,
                "name": name,
                "reason": f"达到止盈目标+{pnl_pct:.0f}%，建议分批止盈",
                "pnl_pct": round(pnl_pct, 1),
                "suggested_price": current_price,
                "action": "分批卖出1/3",
                "priority": "中",
            })
            print(f"  📈 止盈提醒: {name}({code}) 盈利{pnl_pct:.1f}%")

        else:
            result["hold_plan"].append({
                "code": code,
                "name": name,
                "reason": "持有观察",
                "pnl_pct": round(pnl_pct, 1),
                "stop_loss": round(current_price * 0.93, 2),
                "take_profit": round(current_price * 1.15, 2),
            })

    # 7. 仓位汇总
    total_after_buy = current_position_pct + sum(
        b["suggested_position_pct"] for b in result["buy_plan"]
    )
    total_after_sell = sum(
        h.get("市值", 0) for h in holdings if any(
            s["code"] == h.get("代码") for s in result["sell_plan"]
        )
    )
    total_after = total_after_buy - (total_after_sell / total_asset * 100) if total_asset > 0 else total_after_buy

    result["position_summary"] = {
        "current_position_pct": round(current_position_pct, 1),
        "planned_buy_pct": round(sum(b["suggested_position_pct"] for b in result["buy_plan"]), 1),
        "planned_sell_pct": round(total_after_sell / total_asset * 100, 1) if total_asset > 0 else 0,
        "estimated_after_pct": round(total_after, 1),
        "max_allowed_pct": max_position,
    }

    if total_after > max_position:
        result["warnings"].append(
            f"执行后仓位 {total_after:.0f}% 超过允许上限 {max_position}%，建议减少买入或增加卖出"
        )
        print(f"  {warn} 仓位预警: 执行后{total_after:.0f}% > 上限{max_position}%")

    return result


if __name__ == "__main__":
    report = generate_trade_plan()

    print("\n=== TRADE_PLAN ===")
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    print("=== END ===")

    # 保存
    output_dir = os.path.join(os.path.dirname(__file__), "..", "..", "data", "raw")
    os.makedirs(output_dir, exist_ok=True)
    today = datetime.now().strftime("%Y%m%d")
    output_path = os.path.join(output_dir, f"交易原始数据_{today}.json")
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n交易计划已保存: {output_path}")
