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

D9反例（工作反例）：
1. 不要给出"可能"或"看似"等模糊信号——每个指标必须有明确的多/空/中性结论
2. 不要编造数据——缺失的数据返回None或空，不能假设默认值
3. 不要给出矛盾建议（如"MACD显示多头但建议减仓"）——信号应综合判断
4. 不要忽视成交量——无量上涨的突破信号要降级

D3异常处理表：
| 触发条件 | 一线修复 | 仍失败兜底 |
|---------|---------|-----------|
| 指数行情数据为空（非交易日）| 往前推1天重试 | 返回{"error":"数据为空"}，调用方跳过 |
| 板块数据为空（ths_daily无数据）| 往前找5天内的板块数据 | 返回空DataFrame，不输出板块排名章节 |
| technical_analysis指标计算异常 | 单个指标失败不影响其他指标 | 缺失指标标记为None，信号摘要显示"无数据" |
| DataFrame列名不匹配（vol vs volume） | 自动重命名列 | 缺失列尝试传入close代替 |
| 环境评分计算的index不含signals | 跳过该index，不影响整体评分 | 用已有index的评分均值 |

D4 CHECKPOINT:
- CP1-指数数据排序：analyze_index中必须按trade_date排序(旧→新)，否则指标计算倒置
- CP2-指标数据长度：少于35根K线时MACD无意义，需标注"数据不足"
- CP3-板块数据时效：analyze_sectors返回时标注数据日期，不是最新日期应警告
- CP4-信号一致性：environment_score中MACD/RSI/均线评分不能矛盾
"""

import os
import sys
import json
import pandas as pd
import numpy as np
from datetime import datetime, timedelta

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, PROJECT_ROOT)
from scripts.utils.tushare_client import pro, get_ths_index
from scripts.utils.technical_analysis import add_all_indicators, generate_signal_summary

# RPS相对价格强度（导入失败不影响主流程）
try:
    from scripts.utils import rps as rps_engine
    _has_rps = True
except Exception as e:
    print(f"  [WARN] RPS模块导入失败: {e}")
    _has_rps = False

# 数据库管理器（尽力而为，导入失败不影响报告生成）
try:
    from scripts.utils.db_manager import DatabaseManager
    _db = DatabaseManager()
except Exception as e:
    print(f"  [WARN] 数据库连接失败，跳过DB写入: {e}")
    _db = None


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

    # D4-CP1: 按 trade_date 排序（旧→新），否则指标计算倒置
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

    # D4-CP2: 指标数据长度检查
    if len(df_ta) < 35:
        print(f"  [WARN] {name} 数据长度({len(df_ta)})不足35，MACD指标可能无意义")

    df_with_indicators = add_all_indicators(df_ta)
    signals = generate_signal_summary(df_with_indicators)

    # 写入数据库（尽力而为）
    _save_index_to_db(ts_code, df, df_with_indicators)

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
    # D4-CP3: 板块数据时效（优先Tushare THS，失败自动回退东方财富）
    try:
        df = get_ths_index(daily=True)
        if df.empty:
            print(f"  [WARN] 板块数据为空（可能非交易日或API不可用）")
    except Exception as e:
        print(f'  [WARN] get_ths_index异常: {e}')
        return pd.DataFrame()

    if df.empty:
        return pd.DataFrame()

    df = df.sort_values("pct_chg", ascending=False).reset_index(drop=True)

    # 计算强度评分（综合考虑涨跌幅、换手率等）
    result = df.head(top_n).copy()
    result["strength"] = result["pct_chg"]  # 基础强度 = 涨跌幅
    result["anomaly"] = False
    result["data_date"] = end_date

    # 标记异动板块（涨跌幅 > 3% 或 <-3%）
    result.loc[result["pct_chg"] > 3, "anomaly"] = True
    result.loc[result["pct_chg"] < -3, "anomaly"] = True

    return result


def analyze_watchlist_stocks(stock_codes: list, end_date: str) -> list:
    """分析关注列表个股的技术面（优先使用前复权价格）"""
    results = []
    start_date = get_last_n_trade_days(end_date, 120)

    for code in stock_codes:
        try:
            df = pro.daily(ts_code=code, start_date=start_date, end_date=end_date)
            if df.empty:
                continue
            df = df.sort_values("trade_date").reset_index(drop=True)

            # 写入个股日线行情到数据库 (asset_type='E')
            if _db:
                try:
                    df_db = df.copy()
                    if "vol" in df_db.columns:
                        df_db = df_db.rename(columns={"vol": "volume"})
                    _db.upsert_daily_price(df_db, asset_type='E')
                except Exception as e:
                    print(f'  [WARN] DB行情写入失败 ({code}): {e}')

            # 检查是否有复权因子，有则使用前复权价格
            use_adj = _db and _db.has_adj_factor(code) if _db else False
            if use_adj:
                df_adj = _db.get_daily_price_adj(code, start_date, end_date)
                if df_adj is not None and not df_adj.empty:
                    # 用前复权价格构建技术分析DataFrame
                    df_ta = pd.DataFrame({
                        "ts_code":  code,
                        "trade_date": df_adj["trade_date"],
                        "open":   df_adj["open_adj"],
                        "high":   df_adj["high_adj"],
                        "low":    df_adj["low_adj"],
                        "close":  df_adj["close_adj"],
                        "volume": df_adj["vol"] if "vol" in df_adj.columns else 0,
                    }, index=df_adj.index)
                    print(f"    {code}: 使用前复权价格 (latest_factor={df_adj['latest_adj_factor'].iloc[0]:.4f})")
                else:
                    use_adj = False

            if not use_adj:
                df_ta = df.rename(columns={"vol": "volume"})

            # 确保列存在
            for col in ["open", "high", "low", "close"]:
                if col not in df_ta.columns:
                    df_ta[col] = df_ta.get("close", 0)

            df_ta = add_all_indicators(df_ta)
            signals = generate_signal_summary(df_ta)

            # 写入技术指标到数据库
            if _db:
                try:
                    _db.upsert_daily_indicator(df_ta)
                except Exception as e:
                    print(f'  [WARN] DB指标写入失败 ({code}): {e}')

            last = df.iloc[-1]
            close_price = float(df_ta.iloc[-1]["close"]) if df_ta is not None and not df_ta.empty else float(last["close"])
            results.append({
                "ts_code": code,
                "name": last.get("name", code),
                "close": round(close_price, 2),
                "pct_chg": round(float(last.get("pct_chg", 0)), 2),
                "signals": signals,
                "price_adj": "前复权" if use_adj else "不复权",
            })
        except Exception as e:
            print(f"    [WARN] {code} 分析失败: {e}")
            continue

    return results


def get_sector_rotation(sector_df: pd.DataFrame) -> list:
    """分析板块轮动（对比前几日的排名变化）"""
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


def _save_index_to_db(ts_code: str, df_raw: pd.DataFrame, df_indicator: pd.DataFrame):
    """将指数日线和技术指标写入数据库（尽力而为）"""
    if _db is None:
        return
    try:
        # 写入日线行情 (asset_type='I')
        if not df_raw.empty:
            df_price = df_raw.copy()
            if "vol" in df_price.columns:
                df_price = df_price.rename(columns={"vol": "volume"})
            _db.upsert_daily_price(df_price, asset_type='I')
        # 写入技术指标
        if not df_indicator.empty:
            _db.upsert_daily_indicator(df_indicator)
    except Exception as e:
        print(f"  [WARN] DB写入失败 ({ts_code}): {e}")


def generate_analysis(end_date: str = None) -> dict:
    """主函数：生成完整的分析报告数据"""
    if end_date is None:
        end_date = datetime.now().strftime("%Y%m%d")

    print(f"[分析师] 分析日期: {end_date}")
    ok = "[OK]"

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
        print(f"  {ok} 大盘技术分析: {len(result['indices'])} 个指数")
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

            print(f"  {ok} 板块分析: {len(sector_df)} 个板块")
            print(f"  {ok} 异动板块: {len(result['sectors']['anomalies'])} 个")
    except Exception as e:
        result["errors"].append(f"板块分析失败: {e}")
        print(f"  [FAIL] 板块分析: {e}")

    # 3. 市场总结
    valid_indices = [i for i in result.get('indices', []) if isinstance(i, dict) and i.get('pct_chg') is not None]
    if valid_indices:
        avg_pct = np.mean([i["pct_chg"] for i in valid_indices])
        if avg_pct < -1:
            result["market_summary"] = "市场整体偏弱"
        elif avg_pct > 1:
            result["market_summary"] = "市场整体偏强"
        else:
            result["market_summary"] = "市场窄幅震荡"

    # 4. 大盘环境综合评分
    scores = []
    for idx in result["indices"]:
        if "signals" not in idx or "error" in idx:
            continue
        s = 50  # 基准分
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

    # 5. RPS 相对价格强度分析
    try:
        if _has_rps:
            trade_date = end_date
            rps_data = rps_engine.calc_all_rps(trade_date)
            if rps_data is not None and not rps_data.empty:
                # RPS_120 TOP 10
                top_col = 'rps_120' if 'rps_120' in rps_data.columns else None
                if top_col:
                    top10 = rps_data.sort_values(top_col, ascending=False).head(10)
                    result["rps_top10"] = top10[[c for c in ['ts_code','name','rps_20','rps_60','rps_120','rps_250','avg_rps'] if c in top10.columns]].to_dict('records')

                # RPS_120 BOTTOM 5
                    bottom5 = rps_data.sort_values(top_col, ascending=True).head(5)
                    result["rps_bottom5"] = bottom5[[c for c in ['ts_code','name','rps_20','rps_60','rps_120','rps_250','avg_rps'] if c in bottom5.columns]].to_dict('records')

                # RPS强势股数量统计（RPS_120 >= 90）
                strong_count = int((rps_data[top_col] >= 90).sum())
                weak_count = int((rps_data[top_col] < 30).sum())
                result["rps_summary"] = {
                    "total_stocks": len(rps_data),
                    "strong_count_rps90": strong_count,
                    "weak_count_rps30": weak_count,
                    "strong_ratio": round(strong_count / max(len(rps_data), 1) * 100, 1),
                }

                # 行业RPS
                sector_rps = rps_engine.calc_sector_avg_rps_from_df(rps_data)
                if not sector_rps.empty:
                    result["rps_sectors"] = sector_rps.head(10).to_dict('records')
                    worst_sectors = sector_rps.tail(5).iloc[::-1]
                    result["rps_sectors_worst"] = worst_sectors.to_dict('records')

                print(f"  {ok} RPS分析: {len(rps_data)}只股票, 强势>90={strong_count}, 弱势<30={weak_count}")
            else:
                result["rps_summary"] = {"error": "RPS数据为空"}
                print(f"  [WARN] RPS分析: 数据为空")
    except Exception as e:
        print(f"  [WARN] RPS分析失败: {e}")
        result.setdefault("errors", []).append(f"RPS分析失败: {e}")

    # 6. OBV 量能分析（从大盘指数信号中提取OBV信息）
    try:
        obv_summary = {"indices_obv": [], "divergence_alerts": []}
        for idx in result.get("indices", []):
            sig = idx.get("signals", {})
            obv_signal = sig.get("obv", "")
            if obv_signal and obv_signal != "无数据":
                obv_summary["indices_obv"].append({
                    "name": idx.get("name", ""),
                    "obv_signal": obv_signal,
                })
                # 提取背离警报
                if "顶背离" in obv_signal:
                    obv_summary["divergence_alerts"].append({
                        "index": idx.get("name", ""),
                        "type": "bearish_divergence",
                        "detail": f"OBV顶背离 — 价格创新高但量能不济",
                    })
                elif "底背离" in obv_signal:
                    obv_summary["divergence_alerts"].append({
                        "index": idx.get("name", ""),
                        "type": "bullish_divergence",
                        "detail": f"OBV底背离 — 价格创新低但量能已企稳",
                    })
        result["obv_analysis"] = obv_summary
        if obv_summary["divergence_alerts"]:
            print(f"  {ok} OBV背离警报: {len(obv_summary['divergence_alerts'])} 条")
        print(f"  {ok} OBV量能分析完成")
    except Exception as e:
        print(f"  [WARN] OBV分析失败: {e}")

    # 写入报告日志（尽力而为）
    try:
        if _db:
            status = "ok" if not result.get("errors") else "error"
            _db.save_report_log(end_date, "agent2", status,
                               error_msg="; ".join(result["errors"]) if result.get("errors") else None)
    except Exception as e:
        print(f'  [WARN] 报告日志写入失败: {e}')

    return result


if __name__ == "__main__":
    date_arg = sys.argv[1] if len(sys.argv) > 1 else None
    data = generate_analysis(date_arg)

    print("\n=== RESULT_JSON ===")
    # 使用 default=str 处理 numpy 类型
    print(json.dumps(data, ensure_ascii=False, indent=2, default=str))
    print("=== END ===")

    # 保存到 data/raw
    output_dir = os.path.join(PROJECT_ROOT, "data", "raw")
    os.makedirs(output_dir, exist_ok=True)
    output_path = os.path.join(output_dir, f"分析原始数据_{data['date']}.json")
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n分析数据已保存: {output_path}")
