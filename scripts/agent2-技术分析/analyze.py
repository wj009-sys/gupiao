"""
Agent2 分析师 - 技术分析与板块排名核心脚本

功能：
1. 拉取大盘指数的日线数据并计算技术指标
2. 获取同花顺概念板块涨跌排名
3. 计算板块强度评分
4. 识别技术异动信号
5. 输出结构化分析数据供报告生成

用法：
    source venv/Scripts/activate
    python -X utf8 scripts/agent2-技术分析/analyze.py [YYYYMMDD]
"""

import os
import sys
import json
import pandas as pd
import numpy as np
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from scripts.utils.tushare_client import pro
from scripts.utils.technical_analysis import add_all_indicators, generate_signal_summary


def get_last_n_trade_days(end_date: str, n: int = 120) -> str:
    """获取前 N 个交易日的起始日期（大概往前推 2N 天确保够用）"""
    end = datetime.strptime(end_date, "%Y%m%d")
    start = end - timedelta(days=n * 2)
    return start.strftime("%Y%m%d")


def analyze_index(ts_code: str, name: str, end_date: str) -> dict:
    """分析单个指数的技术面"""
    start_date = get_last_n_trade_days(end_date, 120)

    df = pro.index_daily(ts_code=ts_code, start_date=start_date, end_date=end_date)
    if df.empty:
        return {"name": name, "error": "数据为空"}

    # 按 trade_date 排序（旧→新）
    df = df.sort_values("trade_date").reset_index(drop=True)
    # 重命名列以匹配 technical_analysis 的接口
    df_ta = df.rename(columns={
        "open": "open", "high": "high", "low": "low",
        "close": "close", "vol": "volume"
    })
    # 确保列存在
    for col in ["open", "high", "low", "close"]:
        if col not in df_ta.columns:
            df_ta[col] = df_ta.get("close", 0)

    df_with_indicators = add_all_indicators(df_ta)
    signals = generate_signal_summary(df_with_indicators)

    last = df.iloc[-1]
    prev = df.iloc[-2] if len(df) > 1 else last

    # 趋势判断：近5日涨跌幅 + 均线位置
    ma5 = df_with_indicators.iloc[-1].get("ma_5", 0)
    ma20 = df_with_indicators.iloc[-1].get("ma_20", 0)
    ma60 = df_with_indicators.iloc[-1].get("ma_60", 0)
    close = float(last["close"])

    # 均线多头/空头判断
    ma_align = "多头排列" if ma5 > ma20 > ma60 else ("空头排列" if ma5 < ma20 < ma60 else "均线缠绕")

    return {
        "name": name,
        "ts_code": ts_code,
        "close": round(close, 2),
        "pct_chg": round(float(last.get("pct_chg", 0)), 2),
        "volume": float(last.get("vol", last.get("amount", 0))),
        "signals": signals,
        "ma_align": ma_align,
        "ma_5": round(ma5, 2) if ma5 else None,
        "ma_20": round(ma20, 2) if ma20 else None,
        "ma_60": round(ma60, 2) if ma60 else None,
        "above_ma5": close > ma5 if ma5 else None,
        "above_ma20": close > ma20 if ma20 else None,
        "above_ma60": close > ma60 if ma60 else None,
    }


def analyze_sectors(end_date: str, top_n: int = 30) -> pd.DataFrame:
    """获取板块涨跌排名并添加技术面评分"""
    try:
        df = pro.ths_daily(trade_date=end_date)
        if df.empty:
            # 尝试往前找
            for offset in range(1, 5):
                d = (datetime.strptime(end_date, "%Y%m%d") - timedelta(days=offset)).strftime("%Y%m%d")
                df = pro.ths_daily(trade_date=d)
                if not df.empty:
                    break
    except:
        return pd.DataFrame()

    if df.empty:
        return pd.DataFrame()

    df = df.sort_values("pct_chg", ascending=False).reset_index(drop=True)

    # 计算强度评分（综合考虑涨跌幅、换手率等）
    result = df.head(top_n).copy()
    result["strength"] = result["pct_chg"]  # 基础强度 = 涨跌幅
    result["anomaly"] = False

    # 标记异动板块（涨跌幅 > 3% 或 <-3%）
    result.loc[result["pct_chg"] > 3, "anomaly"] = True
    result.loc[result["pct_chg"] < -3, "anomaly"] = True

    return result


def analyze_watchlist_stocks(stock_codes: list, end_date: str) -> list:
    """分析关注列表个股的技术面"""
    results = []
    start_date = get_last_n_trade_days(end_date, 120)

    for code in stock_codes:
        try:
            df = pro.daily(ts_code=code, start_date=start_date, end_date=end_date)
            if df.empty:
                continue
            df = df.sort_values("trade_date").reset_index(drop=True)
            df_ta = df.rename(columns={"vol": "volume"})
            df_ta = add_all_indicators(df_ta)
            signals = generate_signal_summary(df_ta)

            last = df.iloc[-1]
            results.append({
                "ts_code": code,
                "name": last.get("name", code),
                "close": round(float(last["close"]), 2),
                "pct_chg": round(float(last.get("pct_chg", 0)), 2),
                "signals": signals,
            })
        except Exception as e:
            continue

    return results


def get_sector_rotation(sector_df: pd.DataFrame, lookback_days: int = 5) -> list:
    """分析板块轮动（对比前几日的排名变化）"""
    # 板块轮动分析在当前版本中简化处理
    # 直接返回领涨和领跌板块名单
    if sector_df.empty:
        return []

    gainers = sector_df[sector_df["pct_chg"] > 2].head(5)
    losers = sector_df[sector_df["pct_chg"] < -2].tail(5)

    rotation = []
    for _, row in gainers.iterrows():
        rotation.append({
            "name": row["name"],
            "pct_chg": round(row["pct_chg"], 2),
            "direction": "领涨",
        })
    for _, row in losers.iterrows():
        rotation.append({
            "name": row["name"],
            "pct_chg": round(row["pct_chg"], 2),
            "direction": "领跌",
        })

    return rotation


def generate_analysis(end_date: str = None) -> dict:
    """主函数：生成完整的分析报告数据"""
    if end_date is None:
        end_date = datetime.now().strftime("%Y%m%d")

    print(f"[分析师] 分析日期: {end_date}")
    _ok = "[OK]"

    result = {
        "date": end_date,
        "indices": [],
        "sectors": {
            "top_gainers": [],
            "top_losers": [],
            "anomalies": [],
            "rotation": [],
        },
        "watchlist_signals": [],
        "market_summary": "",
        "errors": [],
    }

    # 1. 大盘指数技术分析
    try:
        indices = [
            ("000001.SH", "上证指数"),
            ("399001.SZ", "深证成指"),
            ("399006.SZ", "创业板指"),
            ("000688.SH", "科创50"),
        ]
        for code, name in indices:
            idx_data = analyze_index(code, name, end_date)
            result["indices"].append(idx_data)
        print(f"  {_ok} 大盘技术分析: {len(result['indices'])} 个指数")
    except Exception as e:
        result["errors"].append(f"大盘分析失败: {e}")

    # 2. 板块排名分析
    try:
        sector_df = analyze_sectors(end_date)
        if not sector_df.empty:
            # 领涨板块 TOP10
            gainers = sector_df.head(10)
            result["sectors"]["top_gainers"] = gainers.to_dict("records")

            # 领跌板块 TOP10
            losers = sector_df.tail(10).iloc[::-1]
            result["sectors"]["top_losers"] = losers.to_dict("records")

            # 异动板块（涨跌幅 > 3%）
            anomalies = sector_df[sector_df["anomaly"] == True]
            result["sectors"]["anomalies"] = anomalies.to_dict("records")

            # 板块轮动（领涨/领跌分类）
            result["sectors"]["rotation"] = get_sector_rotation(sector_df)

            print(f"  {_ok} 板块分析: {len(sector_df)} 个板块")
            print(f"  {_ok} 异动板块: {len(result['sectors']['anomalies'])} 个")
    except Exception as e:
        result["errors"].append(f"板块分析失败: {e}")
        print(f"  [FAIL] 板块分析: {e}")

    # 3. 市场总结
    if result["indices"]:
        avg_pct = np.mean([i.get("pct_chg", 0) for i in result["indices"] if "pct_chg" in i])
        if avg_pct < -1:
            result["market_summary"] = "市场整体偏弱"
        elif avg_pct > 1:
            result["market_summary"] = "市场整体偏强"
        else:
            result["market_summary"] = "市场窄幅震荡"

    # 4. 大盘环境综合评分
    scores = []
    for idx in result["indices"]:
        s = 50  # 基准分
        if "signals" not in idx:
            continue
        sig = idx["signals"]
        # MACD 多头加分
        if "多头" in sig.get("macd", ""):
            s += 10
        elif "空头" in sig.get("macd", ""):
            s -= 10
        # RSI 评分
        rsi_text = sig.get("rsi", "")
        if "超卖" in rsi_text:
            s += 5  # 超卖可能反弹
        elif "超买" in rsi_text:
            s -= 5
        # 均线评分
        if idx.get("above_ma20"):
            s += 10
        else:
            s -= 10
        if idx.get("above_ma60"):
            s += 10
        else:
            s -= 10
        scores.append(s)

    if scores:
        result["environment_score"] = round(np.mean(scores), 0)

    return result


if __name__ == "__main__":
    date_arg = sys.argv[1] if len(sys.argv) > 1 else None
    data = generate_analysis(date_arg)

    print("\n=== ANALYSIS_JSON ===")
    # 使用 default=str 处理 numpy 类型
    print(json.dumps(data, ensure_ascii=False, indent=2, default=str))
    print("=== END ===")

    # 保存到 data/raw
    output_dir = os.path.join(os.path.dirname(__file__), "..", "..", "data", "raw")
    os.makedirs(output_dir, exist_ok=True)
    output_path = os.path.join(output_dir, f"分析原始数据_{data['date']}.json")
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n分析数据已保存: {output_path}")
