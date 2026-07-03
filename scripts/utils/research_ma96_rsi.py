"""
MA96 + RSI 策略研究回测

策略逻辑（用户描述）：
  当股价在96线附近，RSI低于30 → 买入（两眼放光）
  大阳线脱离96线，RSI冲上70甚至80 → 卖出（直接给筹码）
  缩量回踩96线，RSI掉到30以下 → 买一点
  放量拉升远离96线，RSI顶到70以上 → 卖一点

核心问题：
  1. 96日线在 A 股是否有统计意义？
  2. RSI(14)在96线附近的极值信号准确率如何？
  3. 缩量/放量确认是否能提高胜率？

用法：
    source venv/Scripts/activate
    python -X utf8 scripts/utils/research_ma96_rsi.py
    python -X utf8 scripts/utils/research_ma96_rsi.py --test-stock 600519.SH  # 指定股票
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import pandas as pd
import numpy as np
from datetime import datetime, timedelta


def compute_strategy_signals(df: pd.DataFrame) -> pd.DataFrame:
    """
    在单个股票的日线DataFrame上计算策略信号

    输入要求：trade_date, close, volume 列，按 trade_date 升序

    返回：
      - signal: 'buy' / 'sell' / ''
      - signal_type: 信号子类型
      - 辅助列: ma96, rsi14, near_ma96, far_from_ma96, vol_ratio
    """
    result = df.copy()
    result = result.sort_values("trade_date").reset_index(drop=True)

    close = result["close"].values
    volume = result["volume"].values if "volume" in result.columns else np.ones(len(result))

    n = len(close)

    # === MA96 ===
    if n >= 96:
        ma96 = pd.Series(close).rolling(window=96, min_periods=60).mean().values
    else:
        ma96 = np.full(n, np.nan)
    result["ma96"] = ma96

    # === RSI(14) ===
    rsi = np.full(n, np.nan)
    if n >= 15:
        delta = np.diff(close)
        gain = np.where(delta > 0, delta, 0)
        loss = np.where(delta < 0, -delta, 0)

        avg_gain = np.full(n, np.nan)
        avg_loss = np.full(n, np.nan)

        avg_gain[14] = np.mean(gain[:14])
        avg_loss[14] = np.mean(loss[:14])

        for i in range(15, n):
            avg_gain[i] = (avg_gain[i - 1] * 13 + gain[i - 1]) / 14
            avg_loss[i] = (avg_loss[i - 1] * 13 + loss[i - 1]) / 14

        rs = avg_gain / np.maximum(avg_loss, 1e-10)
        rsi = 100 - (100 / (1 + rs))
    result["rsi14"] = rsi

    # === 辅助列 ===
    result["near_ma96"] = False     # 股价在96线附近（±3%）
    result["far_from_ma96"] = False  # 股价远离96线（>5%）
    result["vol_ma20"] = np.nan     # 20日均量
    result["vol_ratio"] = np.nan    # 当日量 / 20日均量
    result["is_shrink"] = False     # 缩量（量比 < 0.7）
    result["is_expand"] = False     # 放量（量比 > 1.5）
    result["big_bullish"] = False   # 大阳线（涨幅 > 3%）

    for i in range(1, n):
        # 远离96线判断
        if not np.isnan(ma96[i]) and ma96[i] > 0:
            pct_from_ma = (close[i] - ma96[i]) / ma96[i] * 100
            result.loc[result.index[i], "pct_from_ma96"] = pct_from_ma

            if abs(pct_from_ma) < 3:
                result.loc[result.index[i], "near_ma96"] = True
            if abs(pct_from_ma) > 5:
                result.loc[result.index[i], "far_from_ma96"] = True

        # 均量
        if i >= 20:
            vol_ma20 = np.mean(volume[max(0, i - 20):i])
            result.loc[result.index[i], "vol_ma20"] = vol_ma20
            if vol_ma20 > 0:
                vr = volume[i] / vol_ma20
                result.loc[result.index[i], "vol_ratio"] = vr
                result.loc[result.index[i], "is_shrink"] = vr < 0.7
                result.loc[result.index[i], "is_expand"] = vr > 1.5

        # 大阳线
        pct_chg = (close[i] - close[i - 1]) / close[i - 1] * 100
        result.loc[result.index[i], "pct_chg"] = pct_chg
        result.loc[result.index[i], "big_bullish"] = pct_chg > 3

    # === 信号生成 ===
    signals = np.full(n, "", dtype=object)
    signal_types = np.full(n, "", dtype=object)

    for i in range(20, n):
        if np.isnan(rsi[i]) or np.isnan(ma96[i]) or ma96[i] == 0:
            continue

        near_ma = result.loc[result.index[i], "near_ma96"]
        far_ma = result.loc[result.index[i], "far_from_ma96"]
        shrink = result.loc[result.index[i], "is_shrink"]
        expand = result.loc[result.index[i], "is_expand"]
        big_bull = result.loc[result.index[i], "big_bullish"]
        pct_from_ma = result.loc[result.index[i], "pct_from_ma96"]

        # --- 买入信号 ---
        # 条件A: 股价在96线附近 + RSI < 30 → 超卖 + 支撑
        if near_ma and rsi[i] < 30:
            if shrink:
                signals[i] = "buy"
                signal_types[i] = "缩量回踩96线+RSI超卖"
            else:
                signals[i] = "buy"
                signal_types[i] = "RSI超卖+96线附近"

        # --- 卖出信号 ---
        # 条件B: 大阳线脱离96线 + RSI > 70
        if far_ma and rsi[i] > 70:
            if expand:
                signals[i] = "sell"
                signal_types[i] = "放量拉升远离96线+RSI超买"
            elif big_bull:
                signals[i] = "sell"
                signal_types[i] = "大阳线脱离96线+RSI超买"
            else:
                signals[i] = "sell"
                signal_types[i] = "远离96线+RSI超买"

        # 条件C: RSI > 80 无论位置——极端超买
        if rsi[i] > 80 and not near_ma:
            # 不覆盖已有的sell，但如果已有sell且是更强信号则保留
            if signals[i] != "sell":
                signals[i] = "sell"
                signal_types[i] = "RSI>80极端超买"

    result["signal"] = signals
    result["signal_type"] = signal_types
    return result


def evaluate_strategy(df_signals: pd.DataFrame, initial_capital: float = 100000) -> dict:
    """简单评估策略表现（不考虑手续费、滑点）"""
    result = df_signals.copy()

    capital = initial_capital
    shares = 0
    in_position = False
    entry_price = 0
    entry_date = ""

    trades = []
    equity_curve = [initial_capital]

    for i in range(len(result)):
        row = result.iloc[i]
        signal = row["signal"]
        price = row["close"]
        raw_date = str(row["trade_date"])[:10].replace("-", "")
        # 统一为 YYYYMMDD 格式
        if len(raw_date) == 8 and raw_date.isdigit():
            date = f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:8]}"
        else:
            date = raw_date[:10]

        if signal == "buy" and not in_position:
            shares = capital / price
            entry_price = price
            entry_date = date
            in_position = True
            capital = 0
            trades.append({
                "date": date,
                "type": "buy",
                "price": round(price, 2),
                "shares": round(shares, 0),
                "reason": row["signal_type"],
            })
        elif signal == "sell" and in_position:
            pnl = shares * price - shares * entry_price
            pnl_pct = (price - entry_price) / entry_price * 100
            capital = shares * price
            in_position = False
            trades.append({
                "date": date,
                "type": "sell",
                "price": round(price, 2),
                "shares": round(shares, 0),
                "pnl": round(pnl, 2),
                "pnl_pct": round(pnl_pct, 2),
                "reason": row["signal_type"],
                "days_held": (datetime.strptime(date, "%Y-%m-%d") -
                            datetime.strptime(entry_date, "%Y-%m-%d")).days if date and entry_date else 0,
            })
            shares = 0

        # 每日总资产
        total = capital + shares * price if in_position else capital
        equity_curve.append(total)

    # 最终清算
    if in_position and len(result) > 0:
        final_price = result.iloc[-1]["close"]
        capital = shares * final_price

    total_return = (capital - initial_capital) / initial_capital * 100

    # 统计
    win_trades = [t for t in trades if t["type"] == "sell" and t.get("pnl_pct", 0) > 0]
    lose_trades = [t for t in trades if t["type"] == "sell" and t.get("pnl_pct", 0) <= 0]

    return {
        "total_return_pct": round(total_return, 2),
        "total_trades": len([t for t in trades if t["type"] == "sell"]),
        "win_trades": len(win_trades),
        "lose_trades": len(lose_trades),
        "win_rate": round(len(win_trades) / max(len([t for t in trades if t["type"] == "sell"]), 1) * 100, 1),
        "trades": trades,
        "final_capital": round(capital, 2),
    }


def analyze_ma96_parameter(db_stock_codes: list = None):
    """分析96这个参数本身——为什么96而不是60/100/120"""
    print("\n" + "=" * 60)
    print("📐 参数分析：MA96 从哪来？")
    print("=" * 60)

    analysis = []

    # 不同周期的均线对应的自然时间
    periods = [
        (5, "5日 ≈ 1周"),
        (10, "10日 ≈ 2周"),
        (20, "20日 ≈ 1个月"),
        (60, "60日 ≈ 1个季度"),
        (96, "96日 ≈ 4.5个月 ≈ 半年线"),
        (120, "120日 ≈ 半年"),
        (250, "250日 ≈ 1年"),
    ]

    print(f"\n{'周期':>6} | {'时间跨度':<20} | {'说明'}")
    print("-" * 50)
    for p, desc in periods:
        note = ""
        if p == 96:
            note = "← 非常规参数，介于季线(60)和半年线(120)之间"
        elif p == 20:
            note = "← 月线，A股最常用短期均线"
        elif p == 60:
            note = "← 季线，中期趋势分水岭"
        elif p == 120:
            note = "← 半年线，牛熊分界线"
        print(f"  MA{p:>3} | {desc:<20} | {note}")

    print(f"\n💡 核心发现：")
    print(f"   96 ≈ 4.5 个月（按每月 21 个交易日计算）")
    print(f"   在 A 股中，96 并非标准参数。标准均线参数为 5/10/20/60/120/250")
    print(f"   96 的流行源于 5分钟K线：96×5分钟 = 8小时 = 2个交易日")
    print(f"   在日线中使用 96 日线 ≈ 介于季线(60)和半年线(120)之间的\"中长线\"")

    return analysis


def _fmt_date(d):
    """统一日期格式：转为 YYYY-MM-DD"""
    s = str(d)[:10].replace("-", "")
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    return s[:10]


def analyze_rsi_at_ma(db, stock_codes: list, start_date: str = "20200101", end_date: str = None):
    """分析 RSI 在 MA96 附近的极值表现"""
    from scripts.utils.db_manager import DatabaseManager

    if end_date is None:
        end_date = datetime.now().strftime("%Y%m%d")

    print("\n" + "=" * 60)
    print("🔬 RSI(14) 在 MA96 附近的表现分析")
    print("=" * 60)

    all_signals = {"buy": [], "sell": [], "total_days": 0}

    for code in stock_codes[:20]:  # 限20只
        try:
            df = db.get_daily_price(code, start_date, end_date)
            if df is None or df.empty or len(df) < 120:
                continue

            sig_df = compute_strategy_signals(df)
            all_signals["total_days"] += len(sig_df)

            # 收集买入信号后的短期表现
            for i in range(len(sig_df)):
                if sig_df.iloc[i]["signal"] == "buy":
                    # 看之后5/10/20日的收益
                    future = sig_df.iloc[i: i + 21]
                    if len(future) >= 6:
                        ret5 = (future.iloc[5]["close"] - future.iloc[0]["close"]) / future.iloc[0]["close"] * 100
                        ret10 = (future.iloc[10]["close"] - future.iloc[0]["close"]) / future.iloc[0]["close"] * 100 if len(future) >= 11 else None
                        ret20 = (future.iloc[20]["close"] - future.iloc[0]["close"]) / future.iloc[0]["close"] * 100 if len(future) >= 21 else None
                    else:
                        ret5 = ret10 = ret20 = None

                    all_signals["buy"].append({
                        "code": code,
                        "date": _fmt_date(sig_df.iloc[i]["trade_date"]),
                        "price": round(sig_df.iloc[i]["close"], 2),
                        "rsi": round(sig_df.iloc[i]["rsi14"], 1),
                        "signal_type": sig_df.iloc[i]["signal_type"],
                        "ret_5d": round(ret5, 1) if ret5 is not None else None,
                        "ret_10d": round(ret10, 1) if ret10 is not None else None,
                        "ret_20d": round(ret20, 1) if ret20 is not None else None,
                    })

                if sig_df.iloc[i]["signal"] == "sell":
                    # 看之后5日的表现（卖出后是否回落）
                    future = sig_df.iloc[i: i + 6]
                    if len(future) >= 6:
                        ret5 = (future.iloc[5]["close"] - future.iloc[0]["close"]) / future.iloc[0]["close"] * 100
                    else:
                        ret5 = None

                    all_signals["sell"].append({
                        "code": code,
                        "date": _fmt_date(sig_df.iloc[i]["trade_date"]),
                        "price": round(sig_df.iloc[i]["close"], 2),
                        "rsi": round(sig_df.iloc[i]["rsi14"], 1),
                        "signal_type": sig_df.iloc[i]["signal_type"],
                        "ret_5d": round(ret5, 1) if ret5 is not None else None,
                    })

        except Exception as e:
            continue

    # 汇总统计
    buy_signals = all_signals["buy"]
    sell_signals = all_signals["sell"]

    # 买入信号统计
    buy_5d = [s["ret_5d"] for s in buy_signals if s["ret_5d"] is not None]
    buy_10d = [s["ret_10d"] for s in buy_signals if s["ret_10d"] is not None]
    buy_20d = [s["ret_20d"] for s in buy_signals if s["ret_20d"] is not None]

    sell_5d = [s["ret_5d"] for s in sell_signals if s["ret_5d"] is not None]

    print(f"\n📊 分析样本：{len(stock_codes[:20])} 只股票, {all_signals['total_days']} 个交易日")

    print(f"\n🟢 买入信号统计:")
    print(f"   共 {len(buy_signals)} 次")
    if buy_5d:
        win_5d = sum(1 for r in buy_5d if r > 0)
        print(f"   5日后上涨: {win_5d}/{len(buy_5d)} = {win_5d/len(buy_5d)*100:.1f}% (均涨幅 {np.mean(buy_5d):+.1f}%)")
    if buy_10d:
        win_10d = sum(1 for r in buy_10d if r > 0)
        print(f"   10日后上涨: {win_10d}/{len(buy_10d)} = {win_10d/len(buy_10d)*100:.1f}% (均涨幅 {np.mean(buy_10d):+.1f}%)")
    if buy_20d:
        win_20d = sum(1 for r in buy_20d if r > 0)
        print(f"   20日后上涨: {win_20d}/{len(buy_20d)} = {win_20d/len(buy_20d)*100:.1f}% (均涨幅 {np.mean(buy_20d):+.1f}%)")

    # 信号子类型统计
    type_stats = {}
    for s in buy_signals:
        t = s["signal_type"]
        if t not in type_stats:
            type_stats[t] = {"count": 0, "ret_5d": [], "ret_20d": []}
        type_stats[t]["count"] += 1
        if s["ret_5d"] is not None:
            type_stats[t]["ret_5d"].append(s["ret_5d"])
        if s["ret_20d"] is not None:
            type_stats[t]["ret_20d"].append(s["ret_20d"])

    print(f"\n📋 买入信号子类型:")
    for t, stats in sorted(type_stats.items(), key=lambda x: -x[1]["count"]):
        avg5 = np.mean(stats["ret_5d"]) if stats["ret_5d"] else 0
        avg20 = np.mean(stats["ret_20d"]) if stats["ret_20d"] else 0
        win5 = sum(1 for r in stats["ret_5d"] if r > 0) / max(len(stats["ret_5d"]), 1) * 100 if stats["ret_5d"] else 0
        print(f"   {t:25s} | {stats['count']:3d}次 | 5日均涨幅 {avg5:+.1f}% | 胜率{win5:.0f}%")

    print(f"\n🔴 卖出信号统计:")
    print(f"   共 {len(sell_signals)} 次")
    if sell_5d:
        correct = sum(1 for r in sell_5d if r < 0)  # 卖出后下跌 = 正确
        print(f"   5日后下跌: {correct}/{len(sell_5d)} = {correct/len(sell_5d)*100:.1f}% (均涨幅 {np.mean(sell_5d):+.1f}%)")

    # 卖出子类型
    sell_type_stats = {}
    for s in sell_signals:
        t = s["signal_type"]
        if t not in sell_type_stats:
            sell_type_stats[t] = {"count": 0, "ret_5d": []}
        sell_type_stats[t]["count"] += 1
        if s["ret_5d"] is not None:
            sell_type_stats[t]["ret_5d"].append(s["ret_5d"])

    print(f"\n📋 卖出信号子类型:")
    for t, stats in sorted(sell_type_stats.items(), key=lambda x: -x[1]["count"]):
        avg5 = np.mean(stats["ret_5d"]) if stats["ret_5d"] else 0
        correct = sum(1 for r in stats["ret_5d"] if r < 0) / max(len(stats["ret_5d"]), 1) * 100 if stats["ret_5d"] else 0
        print(f"   {t:30s} | {stats['count']:3d}次 | 5日均涨幅 {avg5:+.1f}% | 卖出正确率{correct:.0f}%")

    return all_signals


def backtest_single_stock(db, code: str, start_date: str = "20200101", end_date: str = None):
    """单只股票完整回测"""
    if end_date is None:
        end_date = datetime.now().strftime("%Y%m%d")

    print(f"\n{'=' * 60}")
    print(f"📈 单股回测: {code}")
    print(f"{'=' * 60}")

    df = db.get_daily_price(code, start_date, end_date)
    if df is None or df.empty or len(df) < 120:
        print(f"  数据不足，跳过")
        return None

    sig_df = compute_strategy_signals(df)
    result = evaluate_strategy(sig_df)

    print(f"   总收益: {result['total_return_pct']:+.2f}%")
    print(f"   交易次数: {result['total_trades']}")
    print(f"   胜率: {result['win_rate']}% ({result['win_trades']}胜/{result['lose_trades']}负)")

    # 打印最近5次交易
    sell_trades = [t for t in result["trades"] if t["type"] == "sell"][-5:]
    if sell_trades:
        print(f"\n   最近{len(sell_trades)}笔卖出交易:")
        for t in sell_trades:
            print(f"     {t['date']} | {t['pnl_pct']:+.1f}% | 持有{t['days_held']}天 | {t['reason']}")

    return result


def compare_ma_periods(db, stock_codes: list, start_date: str = "20200101", end_date: str = None):
    """对比不同均线周期的表现"""
    from scripts.utils.db_manager import DatabaseManager
    if end_date is None:
        end_date = datetime.now().strftime("%Y%m%d")

    print(f"\n{'=' * 60}")
    print(f"📊 均线周期对比回测")
    print(f"{'=' * 60}")

    periods = [20, 60, 96, 120]

    results = {p: {"total_return": [], "win_rates": [], "trade_counts": [], "total_trades": 0} for p in periods}

    for code in stock_codes[:30]:
        df = db.get_daily_price(code, start_date, end_date)
        if df is None or df.empty or len(df) < 250:
            continue

        close = df["close"].values
        volume = df["volume"].values if "volume" in df.columns else np.ones(len(df))

        for period in periods:
            if len(close) < period:
                continue

            # 简化回测：只基于MA位置+RSI，不用volume筛选（公平对比）
            ma = pd.Series(close).rolling(window=period).mean().values
            rsi = _compute_simple_rsi(close, 14)

            capital = 100000
            in_pos = False
            entry_price = 0
            trades = []

            for i in range(period + 20, len(close)):
                if np.isnan(ma[i]) or np.isnan(rsi[i]):
                    continue

                pct_from_ma = (close[i] - ma[i]) / ma[i] * 100
                near_ma = abs(pct_from_ma) < 3
                far_from_ma = abs(pct_from_ma) > 5

                # 买入：MA附近 + RSI < 30
                if not in_pos and near_ma and rsi[i] < 30:
                    entry_price = close[i]
                    in_pos = True

                # 卖出：远离MA + RSI > 70
                elif in_pos and far_from_ma and rsi[i] > 70:
                    pnl = (close[i] - entry_price) / entry_price * 100
                    trades.append(pnl)
                    in_pos = False

            if trades:
                win_rate = sum(1 for t in trades if t > 0) / len(trades) * 100
                results[period]["total_return"].append(sum(trades))
                results[period]["win_rates"].append(win_rate)
                results[period]["trade_counts"].append(len(trades))
                results[period]["total_trades"] += len(trades)

    print(f"\n{'周期':>6} | {'测试股票':>8} | {'总交易':>8} | {'均收益%':>8} | {'均胜率%':>8}")
    print("-" * 50)
    for period in periods:
        r = results[period]
        avg_ret = np.mean(r["total_return"]) if r["total_return"] else 0
        avg_wr = np.mean(r["win_rates"]) if r["win_rates"] else 0
        n_stocks = len(r["total_return"])
        print(f"  MA{period:>3} | {n_stocks:>8d}只 | {r['total_trades']:>8d}次 | {avg_ret:>+7.1f}% | {avg_wr:>7.1f}%")

    return results


def _compute_simple_rsi(close: np.ndarray, period: int = 14) -> np.ndarray:
    """简版RSI计算，供对比回测使用"""
    n = len(close)
    rsi = np.full(n, np.nan)
    if n < period + 1:
        return rsi

    delta = np.diff(close)
    gain = np.where(delta > 0, delta, 0)
    loss = np.where(delta < 0, -delta, 0)

    avg_gain = np.mean(gain[:period])
    avg_loss = np.mean(loss[:period])
    rsi[period] = 100 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss)

    for i in range(period + 1, n):
        avg_gain = (avg_gain * (period - 1) + gain[i - 1]) / period
        avg_loss = (avg_loss * (period - 1) + loss[i - 1]) / period
        rsi[i] = 100 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss)

    return rsi


if __name__ == "__main__":
    from scripts.utils.db_manager import DatabaseManager

    import argparse
    parser = argparse.ArgumentParser(description="MA96 + RSI 策略研究")
    parser.add_argument("--test-stock", type=str, help="单只股票回测代码")
    parser.add_argument("--stocks", type=int, default=10, help="分析股票数量（默认10）")
    parser.add_argument("--start", type=str, default="20200101", help="回测起始日期")
    args = parser.parse_args()

    db = DatabaseManager()

    # 1. 参数分析
    analyze_ma96_parameter()

    # 2. 获取股票列表（从DB取有足够数据的）
    try:
        conn = db.conn
        cursor = conn.cursor()
        cursor.execute('SELECT ts_code FROM daily_price GROUP BY ts_code HAVING count(*) >= 200 ORDER BY count(*) DESC')
        all_rows = cursor.fetchall()
        stock_codes = [r[0] for r in all_rows]
        # 过滤掉指数（以SH/SZ结尾但非纯数字代码）
        stock_codes = [c for c in stock_codes if c.count('.') == 1 and len(c.split('.')[0]) == 6]
        cursor.close()
        print(f"\n📊 可用股票: {len(stock_codes)} 只（≥200条日线数据）")
    except Exception as e:
        print(f"\n⚠️ DB查询失败: {e}")
        stock_codes = ["600519.SH", "000858.SZ", "601318.SH", "600036.SH", "000333.SZ"]

    # 3. 单股回测
    if args.test_stock:
        backtest_single_stock(db, args.test_stock, args.start)
    else:
        # 回测几只代表性股票
        demo_stocks = stock_codes[:min(5, len(stock_codes))]
        for code in demo_stocks:
            backtest_single_stock(db, code, args.start)

    # 4. RSI 极值分析（选前N只）
    print(f"\n{'=' * 60}")
    print(f"⏳ RSI信号统计（{args.stocks}只股票, {args.start}至今）...")
    signal_stats = analyze_rsi_at_ma(db, stock_codes, args.start)

    # 5. 均线周期对比
    compare_ma_periods(db, stock_codes, args.start)

    print(f"\n{'=' * 60}")
    print("✅ 分析完成")
    print(f"{'=' * 60}")
