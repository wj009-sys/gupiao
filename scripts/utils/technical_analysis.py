"""
技术指标计算工具 - 供 Agent2 分析师调用

依赖：pip install ta pandas numpy
"""

import pandas as pd
import numpy as np
import ta


def add_all_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    给DataFrame添加常用技术指标

    要求列名: open, high, low, close, volume (小写)
    返回带指标列的DataFrame
    """
    result = df.copy()

    # MACD (12, 26, 9)
    macd = ta.trend.MACD(close=result["close"])
    result["macd"] = macd.macd()
    result["macd_signal"] = macd.macd_signal()
    result["macd_diff"] = macd.macd_diff()
    result["macd_golden_cross"] = (result["macd"] > result["macd_signal"]) & (
        result["macd"].shift(1) <= result["macd_signal"].shift(1)
    )
    result["macd_death_cross"] = (result["macd"] < result["macd_signal"]) & (
        result["macd"].shift(1) >= result["macd_signal"].shift(1)
    )

    # KDJ (9, 3, 3)
    kdj = ta.momentum.StochasticOscillator(
        high=result["high"], low=result["low"], close=result["close"],
        window=9, smooth_window=3
    )
    result["kdj_k"] = kdj.stoch()
    result["kdj_d"] = kdj.stoch_signal()
    result["kdj_j"] = 3 * result["kdj_k"] - 2 * result["kdj_d"]
    result["kdj_golden_cross"] = (result["kdj_k"] > result["kdj_d"]) & (
        result["kdj_k"].shift(1) <= result["kdj_d"].shift(1)
    )

    # RSI (14)
    result["rsi_14"] = ta.momentum.RSIIndicator(
        close=result["close"], window=14
    ).rsi()
    result["rsi_oversold"] = result["rsi_14"] < 30    # 超卖
    result["rsi_overbought"] = result["rsi_14"] > 70   # 超买

    # 布林带 (20, 2)
    boll = ta.volatility.BollingerBands(
        close=result["close"], window=20, window_dev=2
    )
    result["boll_upper"] = boll.bollinger_hband()
    result["boll_mid"] = boll.bollinger_mavg()
    result["boll_lower"] = boll.bollinger_lband()
    result["boll_width"] = (result["boll_upper"] - result["boll_lower"]) / result["boll_mid"]
    result["boll_break_upper"] = result["close"] > result["boll_upper"]  # 突破上轨
    result["boll_break_lower"] = result["close"] < result["boll_lower"]  # 跌破下轨

    # 移动均线
    result["ma_5"] = ta.trend.SMAIndicator(close=result["close"], window=5).sma_indicator()
    result["ma_10"] = ta.trend.SMAIndicator(close=result["close"], window=10).sma_indicator()
    result["ma_20"] = ta.trend.SMAIndicator(close=result["close"], window=20).sma_indicator()
    result["ma_60"] = ta.trend.SMAIndicator(close=result["close"], window=60).sma_indicator()

    return result


def generate_signal_summary(df: pd.DataFrame) -> dict:
    """
    根据最新一根K线生成信号摘要
    """
    if df.empty:
        return {"error": "数据为空"}

    last = df.iloc[-1]
    prev = df.iloc[-2] if len(df) > 1 else last

    signals = {}

    # MACD信号
    if last.get("macd_golden_cross", False):
        signals["macd"] = "金叉信号 (看多)"
    elif last.get("macd_death_cross", False):
        signals["macd"] = "死叉信号 (看空)"
    elif last.get("macd_diff", 0) > 0:
        signals["macd"] = "多头趋势 (diff>0)"
    else:
        signals["macd"] = "空头趋势 (diff<0)"

    # KDJ信号
    if last.get("kdj_golden_cross", False):
        signals["kdj"] = "金叉信号 (看多)"
    elif last.get("kdj_k", 50) < 20 and last.get("kdj_d", 50) < 20:
        signals["kdj"] = "超卖区 (可能反弹)"
    elif last.get("kdj_k", 50) > 80 and last.get("kdj_d", 50) > 80:
        signals["kdj"] = "超买区 (可能回调)"
    else:
        signals["kdj"] = f"K={last.get('kdj_k', 0):.1f} D={last.get('kdj_d', 0):.1f}"

    # RSI信号
    rsi = last.get("rsi_14", 50)
    if rsi < 30:
        signals["rsi"] = f"超卖 ({rsi:.1f})"
    elif rsi > 70:
        signals["rsi"] = f"超买 ({rsi:.1f})"
    elif rsi > 50:
        signals["rsi"] = f"偏强 ({rsi:.1f})"
    else:
        signals["rsi"] = f"偏弱 ({rsi:.1f})"

    # 布林带信号
    if last.get("boll_break_upper", False):
        signals["boll"] = "突破上轨 (强势)"
    elif last.get("boll_break_lower", False):
        signals["boll"] = "跌破下轨 (弱势)"
    else:
        mid = last.get("boll_mid", 0)
        close = last.get("close", 0)
        if close > mid:
            signals["boll"] = "中轨上方 (偏多)"
        else:
            signals["boll"] = "中轨下方 (偏空)"

    # 均线排列
    ma5, ma10, ma20 = last.get("ma_5", 0), last.get("ma_10", 0), last.get("ma_20", 0)
    if ma5 > ma10 > ma20:
        signals["ma"] = "多头排列 (MA5>MA10>MA20)"
    elif ma5 < ma10 < ma20:
        signals["ma"] = "空头排列 (MA5<MA10<MA20)"
    else:
        signals["ma"] = "均线缠绕"

    return signals


if __name__ == "__main__":
    # 演示：生成示例数据
    import numpy as np
    dates = pd.date_range("2026-01-01", periods=100, freq="D")
    data = {
        "open": np.random.randn(100).cumsum() + 100,
        "high": np.random.randn(100).cumsum() + 102,
        "low": np.random.randn(100).cumsum() + 98,
        "close": np.random.randn(100).cumsum() + 100,
        "volume": np.random.randint(10000, 50000, 100),
    }
    df = pd.DataFrame(data, index=dates)

    df = add_all_indicators(df)
    signals = generate_signal_summary(df)

    print("=== 最新信号摘要 ===")
    for k, v in signals.items():
        print(f"  {k.upper()}: {v}")

    print("\n=== 最后5行指标 ===")
    cols = ["close", "macd", "macd_diff", "kdj_k", "kdj_d", "kdj_j", "rsi_14", "boll_mid"]
    print(df[cols].tail(5).round(2).to_string())
