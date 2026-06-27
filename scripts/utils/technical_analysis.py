"""
技术指标计算工具 - 供 Agent2 分析师调用

依赖：pip install ta pandas numpy

D3异常处理表：
| 触发条件 | 一线修复 | 仍失败兜底 |
|---------|---------|-----------|
| DataFrame为空或None | 检查df.empty，返回空结果 | 调用方用前日数据或标记"数据不可用" |
| 缺少必要列(open/high/low/close/volume) | 检查列名是否存在，缺失列尝试从其他列推导 | 返回原DataFrame，标记缺失指标 |
| 计算指标时KeyError或ValueError(如除零) | 用try/except包裹单个指标计算 | 该指标设为NaN，调用方跳过该信号 |
| 数据量不足(少于60根K线) | 能算多少算多少(如MA5需5根，MACD需35根) | 信号摘要中标注"数据不足" |
| generate_signal_summary访问超出索引 | 用len(df)>1判断，不足时用last=df.iloc[-1] | 返回{"error": "数据不足"} |

D4 CHECKPOINT:
- CP1-输入验证：add_all_indicators开头检查df是否为空、列是否齐全
- CP2-关键值检查：generate_signal_summary检查last.get()的默认值
- CP3-数据脱敏：不存在浮点NaN时，无法计算的指标用None/标记不参与信号

D9反例：
- 不要在未检查df.empty的情况下直接访问df.iloc[-1]
- 不要假设DataFrame包含所有列（外部数据格式可能变化）
- 不要对NaN指标生成看多/看空信号（需过滤或标记为数据不足）
- 不要硬编码指标参数（MACD 12/26/9、KDJ 9/3/3应有文档说明）
"""

import pandas as pd
import numpy as np
import ta


def _safe_indicator(calc_func, name: str, df: pd.DataFrame) -> pd.Series:
    """安全执行指标计算，失败时返回全NaN Series"""
    try:
        return calc_func()
    except Exception as e:
        print(f"[technical_analysis] {name} 计算失败: {e}")
        return pd.Series(np.nan, index=df.index)


def add_all_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    给DataFrame添加常用技术指标

    要求列名: open, high, low, close, volume (小写)
    返回带指标列的DataFrame
    """
    if df is None or df.empty:
        print("[technical_analysis] 输入DataFrame为空，返回空")
        return pd.DataFrame()

    result = df.copy()

    # D4-CP1: 验证必要列是否存在
    required_cols = ["close"]
    for col in required_cols:
        if col not in result.columns:
            print(f"[technical_analysis] 缺少必要列: {col}")
            return pd.DataFrame()  # 无法计算

    has_ohlc = all(c in result.columns for c in ["open", "high", "low", "close", "volume"])

    # MACD (12, 26, 9) — 至少需要35根K线才能算出有意义的值
    if len(result) >= 35:
        macd = _safe_indicator(
            lambda: ta.trend.MACD(close=result["close"]), "MACD", result
        )
        result["macd"] = macd.macd()
        result["macd_signal"] = macd.macd_signal()
        result["macd_diff"] = macd.macd_diff()
        result["macd_golden_cross"] = (result["macd"] > result["macd_signal"]) & (
            result["macd"].shift(1) <= result["macd_signal"].shift(1)
        )
        result["macd_death_cross"] = (result["macd"] < result["macd_signal"]) & (
            result["macd"].shift(1) >= result["macd_signal"].shift(1)
        )
    else:
        print(f"[technical_analysis] 数据长度({len(result)})不足35，跳过MACD计算")

    # KDJ (9, 3, 3) — 需要OHLC数据
    if has_ohlc and len(result) >= 10:
        kdj = _safe_indicator(
            lambda: ta.momentum.StochasticOscillator(
                high=result["high"], low=result["low"], close=result["close"],
                window=9, smooth_window=3
            ), "KDJ", result
        )
        result["kdj_k"] = kdj.stoch()
        result["kdj_d"] = kdj.stoch_signal()
        result["kdj_j"] = 3 * result["kdj_k"] - 2 * result["kdj_d"]
        result["kdj_golden_cross"] = (result["kdj_k"] > result["kdj_d"]) & (
            result["kdj_k"].shift(1) <= result["kdj_d"].shift(1)
        )
    elif has_ohlc:
        print(f"[technical_analysis] 数据长度({len(result)})不足10，跳过KDJ计算")

    # RSI (14)
    if len(result) >= 15:
        rsi_series = _safe_indicator(
            lambda: ta.momentum.RSIIndicator(
                close=result["close"], window=14
            ).rsi(), "RSI", result
        )
        result["rsi_14"] = rsi_series
        if not rsi_series.isna().all():
            result["rsi_oversold"] = result["rsi_14"] < 30
            result["rsi_overbought"] = result["rsi_14"] > 70
        else:
            result["rsi_oversold"] = False
            result["rsi_overbought"] = False
    else:
        print(f"[technical_analysis] 数据长度({len(result)})不足15，跳过RSI计算")

    # 布林带 (20, 2) — 需要至少20根K线
    if len(result) >= 20:
        boll = _safe_indicator(
            lambda: ta.volatility.BollingerBands(
                close=result["close"], window=20, window_dev=2
            ), "BOLL", result
        )
        result["boll_upper"] = boll.bollinger_hband()
        result["boll_mid"] = boll.bollinger_mavg()
        result["boll_lower"] = boll.bollinger_lband()
        result["boll_width"] = (result["boll_upper"] - result["boll_lower"]) / result["boll_mid"]
        result["boll_break_upper"] = result["close"] > result["boll_upper"]
        result["boll_break_lower"] = result["close"] < result["boll_lower"]
    else:
        print(f"[technical_analysis] 数据长度({len(result)})不足20，跳过BOLL计算")

    # 移动均线
    for window, name_suffix in [(5, "5"), (10, "10"), (20, "20"), (60, "60")]:
        if len(result) >= window:
            result[f"ma_{name_suffix}"] = _safe_indicator(
                lambda w=window: ta.trend.SMAIndicator(
                    close=result["close"], window=w
                ).sma_indicator(),
                f"MA{name_suffix}", result
            )
        else:
            print(f"[technical_analysis] 数据长度({len(result)})不足{window}，跳过MA{name_suffix}计算")

    return result


def generate_signal_summary(df: pd.DataFrame) -> dict:
    """
    根据最新一根K线生成信号摘要
    """
    if df is None or df.empty:
        return {"error": "数据为空"}

    # D4-CP2: 检查数据长度是否足够
    if len(df) < 2:
        return {"error": f"数据不足({len(df)}行)，需要至少2行"}

    try:
        last = df.iloc[-1]
        prev = df.iloc[-2] if len(df) > 1 else last
    except Exception as e:
        return {"error": f"数据索引异常: {e}"}

    signals = {}

    # MACD信号
    if last.get("macd_golden_cross", False):
        signals["macd"] = "金叉信号 (看多)"
    elif last.get("macd_death_cross", False):
        signals["macd"] = "死叉信号 (看空)"
    elif not pd.isna(last.get("macd_diff", np.nan)):
        if last["macd_diff"] > 0:
            signals["macd"] = "多头趋势 (diff>0)"
        else:
            signals["macd"] = "空头趋势 (diff<0)"
    else:
        signals["macd"] = "无数据"

    # KDJ信号
    k = last.get("kdj_k", np.nan)
    d = last.get("kdj_d", np.nan)
    if pd.isna(k) or pd.isna(d):
        signals["kdj"] = "无数据"
    elif last.get("kdj_golden_cross", False):
        signals["kdj"] = "金叉信号 (看多)"
    elif k < 20 and d < 20:
        signals["kdj"] = "超卖区 (可能反弹)"
    elif k > 80 and d > 80:
        signals["kdj"] = "超买区 (可能回调)"
    else:
        signals["kdj"] = f"K={k:.1f} D={d:.1f}"

    # RSI信号
    rsi = last.get("rsi_14", np.nan)
    if pd.isna(rsi):
        signals["rsi"] = "无数据"
    elif rsi < 30:
        signals["rsi"] = f"超卖 ({rsi:.1f})"
    elif rsi > 70:
        signals["rsi"] = f"超买 ({rsi:.1f})"
    elif rsi > 50:
        signals["rsi"] = f"偏强 ({rsi:.1f})"
    else:
        signals["rsi"] = f"偏弱 ({rsi:.1f})"

    # 布林带信号
    upper = last.get("boll_break_upper", False)
    lower = last.get("boll_break_lower", False)
    boll_mid = last.get("boll_mid", np.nan)
    close = last.get("close", np.nan)
    if upper:
        signals["boll"] = "突破上轨 (强势)"
    elif lower:
        signals["boll"] = "跌破下轨 (弱势)"
    elif not pd.isna(boll_mid) and not pd.isna(close):
        if close > boll_mid:
            signals["boll"] = "中轨上方 (偏多)"
        else:
            signals["boll"] = "中轨下方 (偏空)"
    else:
        signals["boll"] = "无数据"

    # 均线排列
    ma5 = last.get("ma_5", np.nan)
    ma10 = last.get("ma_10", np.nan)
    ma20 = last.get("ma_20", np.nan)
    if not any(pd.isna(x) for x in [ma5, ma10, ma20]):
        if ma5 > ma10 > ma20:
            signals["ma"] = "多头排列 (MA5>MA10>MA20)"
        elif ma5 < ma10 < ma20:
            signals["ma"] = "空头排列 (MA5<MA10<MA20)"
        else:
            signals["ma"] = "均线缠绕"
    else:
        signals["ma"] = "无数据"

    return signals


if __name__ == "__main__":
    # 演示：生成示例数据
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
    have = [c for c in cols if c in df.columns]
    print(df[have].tail(5).round(2).to_string())

    # 边界情况测试
    print("\n=== 边界测试 ===")
    empty_df = pd.DataFrame()
    print(f"空DataFrame: {generate_signal_summary(empty_df)}")

    single_row = pd.DataFrame({"close": [100]}, index=pd.date_range("2026-06-27", periods=1))
    print(f"单行DataFrame: {generate_signal_summary(single_row)}")
