"""
全策略含退市股回测引擎

回测所有可量化的策略信号，包含退市股以校正幸存者偏差。

用法:
    source venv/Scripts/activate
    python -X utf8 scripts/utils/research_all_strategies.py

流程:
    1. 拉取327只退市股数据（首次需要，后续可跳过）
    2. 从DB读取所有股票（活跃+退市）日线数据
    3. 逐只计算指标、检测信号、追踪表现
    4. 输出汇总统计 → 写入 knowledge/策略/含退市股全策略回测报告.md

可量化的策略清单（来自各策略文件）:
    择时策略.md:      RSI超买/超卖, MA20/MA60突破, 缩量回踩, 放量下跌
    策略规则.json:     MACD金叉/死叉, KDJ超卖金叉, 布林带突破/跌破
    交易执行规则.md:   止盈+15%/20%, 止损-7%, 移动止盈, 时间止损
    选股策略.md:       动量范围[-5%,+30%], 中期趋势(60日>20日), MA多头排列
"""
import sys
import os
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from collections import defaultdict
from typing import List, Dict, Optional

from scripts.utils.db_manager import DatabaseManager
from scripts.utils.tushare_client import pro


# =====================================================================
# Phase 1: 拉取退市股数据
# =====================================================================

def fetch_delisted_stocks_to_db(db: DatabaseManager, max_stocks: int = 350) -> dict:
    """
    从Tushare拉取退市股基础信息和日线数据，写入DB

    Returns: {total, fetched_price, skipped_no_data, errors}
    """
    print("\n" + "=" * 60)
    print("📥 Phase 1: 拉取退市股数据")
    print("=" * 60)

    stats = {"total": 0, "fetched_price": 0, "skipped_no_data": 0, "errors": 0}

    # 1. 获取退市股列表
    print("\n1️⃣  获取退市股列表 (list_status='D')...")
    try:
        basic_df = pro.stock_basic(list_status='D', fields='ts_code,name,market,industry,list_date,delist_date')
        if basic_df is None or basic_df.empty:
            print("  ❌ Tushare 未返回退市股数据")
            return stats
        stats["total"] = len(basic_df)
        print(f"  ✅ 获取 {len(basic_df)} 只退市股")

        # 补全必要字段
        basic_df["list_status"] = "D"
        basic_df["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
        basic_df["industry"] = basic_df["industry"].fillna("")

        # 写入 stock_basic
        n = db.update_stock_basic(basic_df)
        print(f"  ✅ stock_basic 写入 {n} 条")
    except Exception as e:
        print(f"  ❌ 获取退市股列表失败: {e}")
        return stats

    # 2. 逐只拉取日线数据
    print(f"\n2️⃣  逐只拉取日线数据（{len(basic_df)} 只，API 限频 0.2s/次）...")

    # 取得已存在的代码（跳过已拉取的）
    existing = set()
    try:
        cur = db.conn.execute("SELECT DISTINCT ts_code FROM daily_price")
        existing = {r[0] for r in cur.fetchall()}
    except Exception:
        pass

    codes_to_fetch = [r["ts_code"] for _, r in basic_df.iterrows()
                      if r["ts_code"] not in existing]

    print(f"  📊 已有 {len(basic_df) - len(codes_to_fetch)} 只在DB中，待拉取 {len(codes_to_fetch)} 只")

    fetched = 0
    errors = 0
    skipped = 0

    for idx, code in enumerate(codes_to_fetch[:max_stocks]):
        if idx % 20 == 0 and idx > 0:
            print(f"  ... 进度: {idx}/{len(codes_to_fetch)}")

        try:
            df = pro.daily(ts_code=code, start_date="19900101", end_date=datetime.now().strftime("%Y%m%d"))
            if df is not None and not df.empty:
                db.upsert_daily_price(df, asset_type='E')
                fetched += 1
            else:
                skipped += 1
            time.sleep(0.2)  # API 限频
        except Exception as e:
            errors += 1
            print(f"  ⚠️  {code} 拉取失败: {e}")
            time.sleep(0.5)

    stats["fetched_price"] = fetched
    stats["skipped_no_data"] = skipped
    stats["errors"] = errors

    print(f"\n  ✅ 拉取完成: 成功 {fetched}, 空数据 {skipped}, 失败 {errors}")
    return stats


# =====================================================================
# Phase 2: 回测引擎 - 基础计算
# =====================================================================

def _compute_indicators(close: np.ndarray, high: np.ndarray = None,
                        low: np.ndarray = None, volume: np.ndarray = None):
    """
    从日线数据计算所有技术指标

    Returns dict of indicator arrays
    """
    n = len(close)
    result = {}

    # MA
    for p in [5, 10, 20, 60, 120]:
        result[f"ma{p}"] = pd.Series(close).rolling(p, min_periods=max(p // 2, 3)).mean().values

    # RSI(14)
    rsi = np.full(n, np.nan)
    if n >= 15:
        delta = np.diff(close)
        gain = np.where(delta > 0, delta, 0)
        loss = np.where(delta < 0, -delta, 0)
        avg_g = np.full(n, np.nan)
        avg_l = np.full(n, np.nan)
        avg_g[14] = np.mean(gain[:14])
        avg_l[14] = np.mean(loss[:14])
        for i in range(15, n):
            avg_g[i] = (avg_g[i - 1] * 13 + gain[i - 1]) / 14
            avg_l[i] = (avg_l[i - 1] * 13 + loss[i - 1]) / 14
        rs = avg_g / np.maximum(avg_l, 1e-10)
        rsi = 100 - (100 / (1 + rs))
    result["rsi14"] = rsi

    # MACD (12, 26, 9)
    ema12 = pd.Series(close).ewm(span=12).mean().values
    ema26 = pd.Series(close).ewm(span=26).mean().values
    diff = ema12 - ema26
    dea = pd.Series(diff).ewm(span=9).mean().values
    macd_bar = 2 * (diff - dea)
    result["macd_diff"] = diff
    result["macd_dea"] = dea
    result["macd_bar"] = macd_bar

    # KDJ (9, 3, 3)
    kdj_k = np.full(n, 50.0)
    kdj_d = np.full(n, 50.0)
    kdj_j = np.full(n, 50.0)
    if high is not None and low is not None:
        for i in range(9, n):
            hh = np.max(high[i - 8:i + 1])
            ll = np.min(low[i - 8:i + 1])
            if hh > ll:
                rsv = (close[i] - ll) / (hh - ll) * 100
            else:
                rsv = 50
            kdj_k[i] = 2 / 3 * kdj_k[i - 1] + 1 / 3 * rsv
            kdj_d[i] = 2 / 3 * kdj_d[i - 1] + 1 / 3 * kdj_k[i]
            kdj_j[i] = 3 * kdj_k[i] - 2 * kdj_d[i]
    result["kdj_k"] = kdj_k
    result["kdj_d"] = kdj_d
    result["kdj_j"] = kdj_j

    # BOLL (20)
    ma20_val = result["ma20"]
    boll_upper = np.full(n, np.nan)
    boll_mid = np.full(n, np.nan)
    boll_lower = np.full(n, np.nan)
    if n >= 20:
        rolling_std = pd.Series(close).rolling(20).std().values
        boll_mid = ma20_val.copy()
        boll_upper = ma20_val + 2 * rolling_std
        boll_lower = ma20_val - 2 * rolling_std
    result["boll_upper"] = boll_upper
    result["boll_mid"] = boll_mid
    result["boll_lower"] = boll_lower

    # 均量
    if volume is not None:
        vol_ma5 = pd.Series(volume).rolling(5).mean().values
        vol_ma20 = pd.Series(volume).rolling(20).mean().values
        vol_ratio = np.full(n, np.nan)
        for i in range(20, n):
            if vol_ma20[i] > 0:
                vol_ratio[i] = volume[i] / vol_ma20[i]
        result["vol_ma5"] = vol_ma5
        result["vol_ma20"] = vol_ma20
        result["vol_ratio"] = vol_ratio

    # 日涨幅
    pct_chg = np.full(n, np.nan)
    for i in range(1, n):
        if close[i - 1] > 0:
            pct_chg[i] = (close[i] - close[i - 1]) / close[i - 1] * 100
        else:
            pct_chg[i] = 0
    result["pct_chg"] = pct_chg

    # 短期动量: 近20日涨幅
    mom_20 = np.full(n, np.nan)
    for i in range(20, n):
        if close[i - 20] > 0:
            mom_20[i] = (close[i] - close[i - 20]) / close[i - 20] * 100
    result["mom_20"] = mom_20

    # 中期趋势: 近60日涨幅
    mom_60 = np.full(n, np.nan)
    for i in range(60, n):
        if close[i - 60] > 0:
            mom_60[i] = (close[i] - close[i - 60]) / close[i - 60] * 100
    result["mom_60"] = mom_60

    return result


def _get_future_returns(close: np.ndarray, idx: int, periods: List[int]) -> Dict[int, float]:
    """获取未来N日的收益"""
    result = {}
    for p in periods:
        end = idx + p
        if end < len(close) and close[idx] > 0:
            result[p] = (close[end] - close[idx]) / close[idx] * 100
    return result


def _classify_stock(ts_code: str, db) -> str:
    """判断股票类型: active / delisted"""
    cur = db.conn.execute("SELECT list_status FROM stock_basic WHERE ts_code = ?", (ts_code,))
    r = cur.fetchone()
    if r is None:
        return "unknown"
    return "delisted" if r[0] == "D" else "active"


# =====================================================================
# Phase 2a: 技术信号回测
# =====================================================================

def _detect_all_signals(close, high, low, volume, indicators: dict, n: int):
    """
    在单只股票上检测所有策略信号，返回信号列表

    每项信号: {date_idx, signal_type, detail, price, indicators_at_signal}
    """
    signals = []

    for i in range(120, n):  # 需要足够数据
        rsi = indicators["rsi14"][i]
        ma20 = indicators["ma20"][i]
        ma60 = indicators["ma60"][i]
        ma5 = indicators["ma5"][i]
        ma10 = indicators["ma10"][i]
        macd_diff = indicators["macd_diff"][i]
        macd_dea = indicators["macd_dea"][i]
        kdj_k = indicators["kdj_k"][i]
        kdj_d = indicators["kdj_d"][i]
        boll_mid = indicators["boll_mid"][i]
        boll_upper = indicators["boll_upper"][i]
        boll_lower = indicators["boll_lower"][i]
        vol_ratio = indicators.get("vol_ratio", np.full(n, np.nan))[i]
        pct_chg = indicators["pct_chg"][i]
        mom_20 = indicators["mom_20"][i]
        ma120 = indicators.get("ma120", np.full(n, np.nan))[i]

        if np.isnan(rsi) or np.isnan(ma20) or np.isnan(ma60):
            continue

        signal_group = {}

        # === 技术择时信号 ===

        # 1. RSI超卖 (择时策略: RSI<30 → 超卖反弹机会)
        if rsi < 30:
            signal_group["rsi_buy"] = {"type": "rsi_oversold_buy",
                                        "detail": f"RSI({rsi:.0f})<30",
                                        "strength": "strong" if rsi < 25 else "medium"}

        # 2. RSI超买 (择时策略: RSI>70 → 超买区域)
        if rsi > 70:
            signal_group["rsi_sell"] = {"type": "rsi_overbought_sell",
                                         "detail": f"RSI({rsi:.0f})>70",
                                         "strength": "strong" if rsi > 75 else "medium"}

        # 3. RSI从30以下回升 (策略规则.json)
        if i >= 1 and rsi > 30 and indicators["rsi14"][i - 1] is not None and not np.isnan(indicators["rsi14"][i - 1]):
            if indicators["rsi14"][i - 1] < 30 and rsi > indicators["rsi14"][i - 1]:
                signal_group["rsi_recovery"] = {"type": "rsi_from_oversold_recovery",
                                                 "detail": f"RSI从{indicators['rsi14'][i-1]:.0f}回升至{rsi:.0f}"}

        # 4. MACD零轴上方金叉 (策略规则.json)
        if i >= 1:
            prev_diff = indicators["macd_diff"][i - 1]
            prev_dea = indicators["macd_dea"][i - 1]
            if not np.isnan(macd_diff) and not np.isnan(macd_dea) and not np.isnan(prev_diff) and not np.isnan(prev_dea):
                if prev_diff < prev_dea and macd_diff > macd_dea and macd_diff > 0:
                    signal_group["macd_golden_cross"] = {"type": "macd_golden_cross_above_zero",
                                                          "detail": "MACD零轴上方金叉"}

        # 5. MACD死叉 (策略规则.json)
        if i >= 1:
            if not np.isnan(macd_diff) and not np.isnan(macd_dea) and not np.isnan(prev_diff) and not np.isnan(prev_dea):
                if prev_diff > prev_dea and macd_diff < macd_dea and macd_diff < 0:
                    signal_group["macd_death_cross"] = {"type": "macd_death_cross_below_zero",
                                                         "detail": "MACD零轴下方死叉"}

        # 6. KDJ超卖区金叉 (策略规则.json: KDJ超卖区金叉)
        if i >= 1:
            prev_k = indicators["kdj_k"][i - 1]
            prev_d = indicators["kdj_d"][i - 1]
            if not np.isnan(kdj_k) and not np.isnan(kdj_d) and not np.isnan(prev_k) and not np.isnan(prev_d):
                if prev_k < prev_d and kdj_k > kdj_d and kdj_k < 20:
                    signal_group["kdj_oversold_cross"] = {"type": "kdj_oversold_golden_cross",
                                                           "detail": f"KDJ超卖金叉(K={kdj_k:.0f})"}

        # 7. 放量突破布林带中轨 (策略规则.json)
        if not np.isnan(boll_mid) and not np.isnan(close[i]):
            if close[i] > boll_mid and close[i - 1] <= boll_mid if i >= 1 else close[i] > boll_mid:
                if not np.isnan(vol_ratio) and vol_ratio > 1.5:
                    signal_group["boll_breakout"] = {"type": "boll_breakout_with_volume",
                                                      "detail": f"放量突破布林带中轨(量比{vol_ratio:.1f})"}

        # 8. 跌破布林带下轨 (策略规则.json)
        if not np.isnan(boll_lower):
            if close[i] < boll_lower:
                signal_group["boll_breakdown"] = {"type": "boll_break_below_lower",
                                                   "detail": "跌破布林带下轨"}

        # === 择时策略信号 ===

        # 9. 缩量回踩MA20不破 (择时策略: 回调不破支撑)
        if not np.isnan(ma20) and not np.isnan(close[i]):
            pct_from_ma20 = (close[i] - ma20) / ma20 * 100
            if -2 < pct_from_ma20 < 0.5 and not np.isnan(vol_ratio) and vol_ratio < 0.7:
                signal_group["ma20_shrink_hold"] = {"type": "shrink_to_ma20_hold",
                                                     "detail": f"缩量回踩MA20不破(量比{vol_ratio:.1f})"}

        # 10. 放量下跌 (择时策略: 放量200%+阴线=清仓)
        if not np.isnan(pct_chg) and not np.isnan(vol_ratio):
            if pct_chg < -3 and vol_ratio > 2.0:
                signal_group["volume_panic_sell"] = {"type": "volume_panic_sell",
                                                      "detail": f"放量下跌{vol_ratio:.1f}倍量"}

        # 11. 大盘跌破MA20 (个股层面检查 - 跌破MA20减仓)
        if not np.isnan(ma20) and not np.isnan(close[i]) and i >= 5:
            ma5_val = indicators["ma5"][i]
            if close[i] < ma20 and (ma5_val < ma20 or close[i - 1] >= ma20 if not np.isnan(close[i - 1]) else True):
                # 5日线死叉20日线 或 刚跌破20日线
                signal_group["ma20_breakdown"] = {"type": "ma20_breakdown",
                                                   "detail": "跌破MA20(减仓信号)"}

        # 12. MA60跌破
        if not np.isnan(ma60) and close[i] < ma60:
            if i >= 1 and close[i - 1] >= ma60:
                signal_group["ma60_breakdown"] = {"type": "ma60_breakdown",
                                                   "detail": "跌破MA60(清仓信号)"}

        # === 选股因子信号 ===

        # 13. 短期动量范围筛选 (选股策略: [-5%, +30%])
        if not np.isnan(mom_20):
            if -5 <= mom_20 <= 30:
                signal_group["momentum_pass"] = {"type": "momentum_filter_pass",
                                                  "detail": f"短期动量{mom_20:.0f}%"}

        # 14. 中期趋势延续 (选股策略: 60日涨幅 > 20日涨幅)
        if not np.isnan(mom_20) and not np.isnan(indicators["mom_60"][i]):
            if indicators["mom_60"][i] > mom_20:
                signal_group["trend_continuation"] = {"type": "trend_60_gt_20",
                                                       "detail": "中期趋势延续(60日>20日)"}

        # 15. MA多头排列 (选股策略: 5>10>20>60)
        if not np.isnan(ma5) and not np.isnan(ma10) and not np.isnan(ma20) and not np.isnan(ma60):
            if ma5 > ma10 > ma20 > ma60:
                signal_group["ma_bullish"] = {"type": "ma_bullish_alignment",
                                               "detail": "MA多头排列(5>10>20>60)"}

        # 记录所有检测到的信号
        for key, sig in signal_group.items():
            signals.append({
                "idx": i,
                "price": close[i],
                **sig
            })

    return signals


def _evaluate_signal_performance(close: np.ndarray, signals: list, forward_periods=None):
    """
    评估信号后的收益表现

    对买入信号: 看5/10/20日后收益
    对卖出信号: 看卖出后5日是否下跌
    """
    if forward_periods is None:
        forward_periods = [5, 10, 20]

    results = defaultdict(list)

    for sig in signals:
        idx = sig["idx"]
        type_key = sig["type"]

        # 买入类信号: 看未来收益
        if any(kw in type_key for kw in ["buy", "cross", "hold", "pass", "continuation", "bullish", "recovery"]):
            returns = _get_future_returns(close, idx, forward_periods)
            if returns:
                results[type_key].append({
                    "return_5d": returns.get(5),
                    "return_10d": returns.get(10),
                    "return_20d": returns.get(20),
                    "price": sig["price"],
                    "detail": sig.get("detail", ""),
                })

        # 卖出类信号: 看卖出后是否下跌
        if any(kw in type_key for kw in ["sell", "death", "breakdown", "panic"]):
            returns = _get_future_returns(close, idx, [5])
            if returns:
                results[type_key].append({
                    "return_5d": returns.get(5),
                    "price": sig["price"],
                    "detail": sig.get("detail", ""),
                })

    return dict(results)


# =====================================================================
# Phase 2b: 交易规则回测
# =====================================================================

def _simulate_trading_rules(close: np.ndarray, pct_chg: np.ndarray,
                            trade_dates: list, stock_type: str) -> dict:
    """
    模拟交易规则执行效果（在每只股票上逐个价格点模拟买入持有）

    策略:
    - 买入: 模拟在随机/固定点买入（用rolling window采买入点）
    - 止损: -7% fixed stop-loss
    - 止盈: +15%/+20% take profit
    - 移动止盈: +10%后回撤-5%
    - 时间止损: 5日不达预期
    """
    n = len(close)
    if n < 60:
        return {}

    results = {
        "no_stop": {"trades": [], "returns": []},           # 不止损
        "fixed_stop_7": {"trades": [], "returns": []},      # -7%止损
        "take_profit_15_20": {"trades": [], "returns": []}, # +15%~+20%分批止盈
        "trailing_stop": {"trades": [], "returns": []},     # 移动止盈
        "time_stop_5d": {"trades": [], "returns": []},      # 时间止损
    }

    # 每20个交易日取一个买入点（模拟定期买入）
    entry_points = list(range(120, n - 30, 20))

    for entry_idx in entry_points:
        entry_price = close[entry_idx]

        # === 规则1: 不止损（简单持有）===
        exit_idx = entry_idx + 20  # 持有20日
        if exit_idx < n:
            ret = (close[exit_idx] - entry_price) / entry_price * 100
            results["no_stop"]["trades"].append(1)
            results["no_stop"]["returns"].append(ret)

        # === 规则2: 固定止损-7% ===
        result = _simulate_stop_loss(close, entry_idx, pct_chg, -7, 20)
        results["fixed_stop_7"]["trades"].append(1)
        results["fixed_stop_7"]["returns"].append(result)

        # === 规则3: +15%/20% 分批止盈 ===
        result = _simulate_take_profit(close, entry_idx, 20)
        results["take_profit_15_20"]["trades"].append(1)
        results["take_profit_15_20"]["returns"].append(result)

        # === 规则4: 移动止盈 (盈利>10%后回撤-5%) ===
        result = _simulate_trailing_stop(close, entry_idx, 20)
        results["trailing_stop"]["trades"].append(1)
        results["trailing_stop"]["returns"].append(result)

        # === 规则5: 时间止损 (5日不达预期) ===
        result = _simulate_time_stop(close, entry_idx, pct_chg, 5)
        results["time_stop_5d"]["trades"].append(1)
        results["time_stop_5d"]["returns"].append(result)

    return results


def _simulate_stop_loss(close: np.ndarray, entry_idx: int,
                        pct_chg: np.ndarray, stop_pct: float,
                        max_hold: int) -> float:
    """模拟固定止损: 触发即卖，否则持有max_hold天"""
    entry_price = close[entry_idx]
    for i in range(entry_idx + 1, min(entry_idx + max_hold + 1, len(close))):
        pnl = (close[i] - entry_price) / entry_price * 100
        if pnl <= stop_pct:
            return pnl  # 触发止损
    # 未触发止损，持有到期
    end = min(entry_idx + max_hold, len(close) - 1)
    return (close[end] - entry_price) / entry_price * 100


def _simulate_take_profit(close: np.ndarray, entry_idx: int, max_hold: int) -> float:
    """
    模拟分批止盈:
    - 达到+15%: 卖1/3
    - 达到+20%: 卖1/3 (累计2/3)
    - 剩余1/3: 持有至max_hold
    """
    entry_price = close[entry_idx]
    end = min(entry_idx + max_hold, len(close) - 1)

    sold_third = False  # 是否已卖第1批
    sold_two_thirds = False  # 是否已卖第2批

    for i in range(entry_idx + 1, end + 1):
        pnl = (close[i] - entry_price) / entry_price * 100
        if not sold_third and pnl >= 15:
            sold_third = True
        if sold_third and not sold_two_thirds and pnl >= 20:
            sold_two_thirds = True

    # 最终收益 = 分批加权
    final_pnl = (close[end] - entry_price) / entry_price * 100
    if sold_two_thirds:
        return 0.33 * 15 + 0.33 * 20 + 0.34 * final_pnl
    elif sold_third:
        return 0.33 * 15 + 0.67 * final_pnl
    else:
        return final_pnl


def _simulate_trailing_stop(close: np.ndarray, entry_idx: int, max_hold: int) -> float:
    """模拟移动止盈: 盈利>10%后从最高点回撤-5%即卖"""
    entry_price = close[entry_idx]
    peak = entry_price
    end = min(entry_idx + max_hold, len(close) - 1)

    for i in range(entry_idx + 1, end + 1):
        pnl = (close[i] - entry_price) / entry_price * 100
        if close[i] > peak:
            peak = close[i]
        # 如果盈利超过10%，启用移动止盈
        if (peak - entry_price) / entry_price * 100 > 10:
            drawdown = (close[i] - peak) / peak * 100
            if drawdown <= -5:
                return (close[i] - entry_price) / entry_price * 100

    return (close[end] - entry_price) / entry_price * 100


def _simulate_time_stop(close: np.ndarray, entry_idx: int,
                        pct_chg: np.ndarray, max_flat_days: int) -> float:
    """
    模拟时间止损:
    买入后max_flat_days个交易日，如果价格变化在±1%以内（不达预期），减半仓
    然后继续持有剩余max_hold天
    """
    entry_price = close[entry_idx]
    max_hold = 20

    # 检查5天是否"不达预期"
    check_idx = min(entry_idx + max_flat_days, len(close) - 1)
    pnl_at_check = (close[check_idx] - entry_price) / entry_price * 100

    end = min(entry_idx + max_hold, len(close) - 1)
    final_pnl = (close[end] - entry_price) / entry_price * 100

    if abs(pnl_at_check) < 1:  # 不达预期
        # 减半仓: 50%在这里卖，50%持有到end
        return 0.5 * pnl_at_check + 0.5 * final_pnl
    else:
        return final_pnl


# =====================================================================
# Phase 3: 汇总统计
# =====================================================================

def _summarize_returns(returns_list: list) -> dict:
    """汇总一组收益率的统计"""
    arr = np.array(returns_list)
    if len(arr) == 0:
        return {"count": 0, "mean": 0, "median": 0, "win_rate": 0,
                "std": 0, "max": 0, "min": 0, "sharpe": 0}

    mean_ret = np.mean(arr)
    win_rate = np.sum(arr > 0) / len(arr) * 100
    sharpe = mean_ret / np.std(arr) * np.sqrt(252/20) if np.std(arr) > 0 else 0

    return {
        "count": len(arr),
        "mean": round(mean_ret, 2),
        "median": round(np.median(arr), 2),
        "win_rate": round(win_rate, 1),
        "std": round(np.std(arr), 2),
        "max": round(np.max(arr), 2),
        "min": round(np.min(arr), 2),
        "sharpe": round(sharpe, 2),
        "p_25": round(np.percentile(arr, 25), 2),
        "p_75": round(np.percentile(arr, 75), 2),
    }


def _compare_active_vs_delisted(results: dict) -> dict:
    """
    对比活跃股 vs 退市股的信号表现

    results结构: {ts_code: {stock_type, signals: {type: [perf]}}}
    """
    comparison = defaultdict(lambda: {"active": defaultdict(list), "delisted": defaultdict(list)})

    for code, data in results.items():
        stype = data.get("stock_type", "active")
        for signal_type, perf_list in data.get("signals", {}).items():
            comparison[signal_type][stype].extend(perf_list)

    summary = {}
    for signal_type, groups in comparison.items():
        active = groups["active"]
        delisted = groups["delisted"]
        active_stats = _summarize_returns([p.get("return_5d", 0) for p in active]) if active else {"count": 0}
        delisted_stats = _summarize_returns([p.get("return_5d", 0) for p in delisted]) if delisted else {"count": 0}
        summary[signal_type] = {
            "active": active_stats,
            "delisted": delisted_stats,
        }
    return summary


# =====================================================================
# 主回测流程
# =====================================================================

def run_full_backtest(db: DatabaseManager, max_stocks: int = 1000,
                      force_fetch: bool = False, sample_delisted: int = None):
    """
    全策略含退市股回测主流程

    Args:
        db: 数据库管理器
        max_stocks: 最大回测股票数（活跃股）
        force_fetch: 是否强制重新拉取退市股数据
        sample_delisted: 退市股取样数（None=全部）
    """
    print("\n" + "=" * 60)
    print("🧪 全策略含退市股回测")
    print("=" * 60)

    # === Phase 1: 数据准备 ===
    print("\n" + "=" * 60)
    print("📦 Phase 0: 数据准备")
    print("=" * 60)

    # 检查已有退市股数据
    cur = db.conn.execute("SELECT COUNT(*) FROM daily_price d JOIN stock_basic s ON d.ts_code = s.ts_code WHERE s.list_status = 'D'")
    existing_delisted = cur.fetchone()[0]

    if existing_delisted == 0 or force_fetch:
        fetch_delisted_stocks_to_db(db)
    else:
        print(f"  ✅ 退市股数据已存在，跳过拉取")

    # 获取股票列表
    codes_active = []
    codes_delisted = []

    # 活跃股：有≥250行日线数据
    cur = db.conn.execute("""
        SELECT d.ts_code FROM daily_price d
        JOIN stock_basic s ON d.ts_code = s.ts_code
        WHERE s.list_status = 'L'
        GROUP BY d.ts_code HAVING COUNT(*) >= 250
        ORDER BY COUNT(*) DESC
    """)
    codes_active = [r[0] for r in cur.fetchall()[:max_stocks]]

    # 退市股：有≥50行日线数据
    cur = db.conn.execute("""
        SELECT d.ts_code FROM daily_price d
        JOIN stock_basic s ON d.ts_code = s.ts_code
        WHERE s.list_status = 'D'
        GROUP BY d.ts_code HAVING COUNT(*) >= 50
        ORDER BY COUNT(*) DESC
    """)
    all_delisted = [r[0] for r in cur.fetchall()]
    codes_delisted = all_delisted[:sample_delisted] if sample_delisted else all_delisted

    print(f"\n  📊 活跃股: {len(codes_active)} 只 (≥250行数据)")
    print(f"  📊 退市股: {len(codes_delisted)} 只 (≥50行数据, 共{len(all_delisted)}只有数据)")

    all_stocks = codes_active + codes_delisted

    # === Phase 2: 逐只回测 ===
    print(f"\n{'=' * 60}")
    print(f"🔬 Phase 2: 逐只回测 ({len(all_stocks)} 只)")
    print(f"{'=' * 60}")

    # 收集结果
    signal_stats = defaultdict(lambda: defaultdict(list))  # {signal_type: {active/delisted: [ret_5d]}}
    trade_rule_results = defaultdict(lambda: defaultdict(list))
    signal_detail = defaultdict(lambda: {"active": [], "delisted": []})

    processed = 0
    skipped = 0
    errors = 0

    for idx, code in enumerate(all_stocks):
        if idx > 0 and idx % 50 == 0:
            print(f"  📊 进度: {idx}/{len(all_stocks)} 只 (已处理{processed}, 跳过{skipped}, 错误{errors})")

        stock_type = "delisted" if code in codes_delisted else "active"

        try:
            df = db.get_daily_price(code, "20000101", datetime.now().strftime("%Y%m%d"))
            if df is None or df.empty or len(df) < 120:
                skipped += 1
                continue

            # 确保按日期排序
            df = df.sort_values("trade_date").reset_index(drop=True)
            close = df["close"].values
            high = df["high"].values
            low = df["low"].values
            volume = df["vol"].values if "vol" in df.columns else np.ones(len(df))
            trade_dates = df["trade_date"].tolist()

            # 计算指标
            indicators = _compute_indicators(close, high, low, volume)

            # 检测信号
            signals = _detect_all_signals(close, high, low, volume, indicators, len(close))

            # 评估信号表现
            signal_perf = _evaluate_signal_performance(close, signals)

            # 收集统计
            for sig_type, perf_list in signal_perf.items():
                for p in perf_list:
                    if p.get("return_5d") is not None:
                        signal_stats[sig_type][stock_type].append(p["return_5d"])
                        signal_detail[sig_type][stock_type].append({
                            "code": code,
                            "price": p["price"],
                            "ret_5d": p["return_5d"],
                            "ret_10d": p.get("return_10d"),
                            "ret_20d": p.get("return_20d"),
                            "detail": p.get("detail", ""),
                        })

            # 交易规则模拟
            pct_chg_arr = indicators["pct_chg"]
            rule_results = _simulate_trading_rules(close, pct_chg_arr, trade_dates, stock_type)
            for rule_name, rule_data in rule_results.items():
                trade_rule_results[rule_name][stock_type].extend(rule_data["returns"])

            processed += 1

        except Exception as e:
            errors += 1
            if errors <= 5:
                print(f"  ⚠️  {code} 回测失败: {e}")
            continue

    print(f"\n  ✅ 回测完成: 处理 {processed}, 跳过 {skipped}, 错误 {errors}")

    # === Phase 3: 汇总分析 ===
    print(f"\n{'=' * 60}")
    print(f"📊 Phase 3: 汇总分析")
    print(f"{'=' * 60}")

    report_sections = {}

    # --- 技术信号表现 ---
    print(f"\n{'=' * 60}")
    print("🟢 技术择时信号回测结果")
    print("=" * 60)

    sorted_signals = sorted(signal_stats.keys())
    print(f"\n{'信号类型':<40} | {'活跃股':>8} | {'退市股':>8} | {'整体胜率':>8} | {'整体均收益':>10}")
    print("-" * 85)

    report_lines = ["| 信号 | 活跃股胜率 | 活跃股均收益 | 退市股胜率 | 退市股均收益 | 整体胜率 | 整体均收益 |"]
    report_lines.append("|:----|:---------:|:-----------:|:---------:|:-----------:|:-------:|:---------:|")

    signal_ranking = []  # 按5日胜率排序

    for sig_type in sorted_signals:
        active = signal_stats[sig_type].get("active", [])
        delisted = signal_stats[sig_type].get("delisted", [])
        all_returns = active + delisted

        if not all_returns:
            continue

        a_stats = _summarize_returns(active)
        d_stats = _summarize_returns(delisted)
        total_stats = _summarize_returns(all_returns)

        # 信号中文名映射
        name_map = {
            "rsi_oversold_buy": "RSI<30买入",
            "rsi_overbought_sell": "RSI>70卖出(5日后)",
            "rsi_from_oversold_recovery": "RSI从30以下回升买入",
            "macd_golden_cross_above_zero": "MACD零轴上方金叉买入",
            "macd_death_cross_below_zero": "MACD零轴下方死叉卖出(5日后)",
            "kdj_oversold_golden_cross": "KDJ超卖金叉(K<20)买入",
            "boll_breakout_with_volume": "放量突破布林带中轨买入",
            "boll_break_below_lower": "跌破布林带下轨卖出(5日后)",
            "shrink_to_ma20_hold": "缩量回踩MA20不破买入",
            "volume_panic_sell": "放量下跌卖出(5日后)",
            "ma20_breakdown": "跌破MA20卖出(5日后)",
            "ma60_breakdown": "跌破MA60清仓(5日后)",
            "momentum_filter_pass": "短期动量[-5%,+30%]筛选",
            "trend_60_gt_20": "中期趋势延续(60日>20日)",
            "ma_bullish_alignment": "MA多头排列(5>10>20>60)",
        }
        sig_name = name_map.get(sig_type, sig_type)

        # 判断信号方向
        is_sell_signal = any(kw in sig_type for kw in ["sell", "death", "breakdown", "panic"])

        # 对卖出信号：负收益=正确（卖出后下跌），正收益=错误（卖出后上涨）
        if is_sell_signal:
            a_accuracy = sum(1 for r in active if r < 0) / max(len(active), 1) * 100
            d_accuracy = sum(1 for r in delisted if r < 0) / max(len(delisted), 1) * 100
            t_accuracy = sum(1 for r in all_returns if r < 0) / max(len(all_returns), 1) * 100
            a_line = f"{a_accuracy:>7.1f}%"
            d_line = f"{d_accuracy:>7.1f}%"
            t_line = f"{t_accuracy:>7.1f}%"
            mean_display = f"{total_stats['mean']:+.1f}%"
        else:
            a_line = f"{a_stats['win_rate']:>7.1f}%"
            d_line = f"{d_stats['win_rate']:>7.1f}%"
            t_line = f"{total_stats['win_rate']:>7.1f}%"
            mean_display = f"{total_stats['mean']:+.2f}%"

        a_mean = f"{a_stats['mean']:+.2f}%"
        d_mean = f"{d_stats['mean']:+.2f}%"

        print(f"  {sig_name:<38} | {a_line} | {d_line} | {t_line} | {mean_display:>10}")
        report_lines.append(f"| {sig_name} | {a_line} | {a_mean} | {d_line} | {d_mean} | {t_line} | {mean_display} |")

        signal_ranking.append({
            "name": sig_name,
            "type": sig_type,
            "is_sell": is_sell_signal,
            "active_win_rate": a_stats["win_rate"],
            "delisted_win_rate": d_stats["win_rate"],
            "active_accuracy": sum(1 for r in active if r < 0) / max(len(active), 1) * 100 if active else 0,
            "delisted_accuracy": sum(1 for r in delisted if r < 0) / max(len(delisted), 1) * 100 if delisted else 0,
            "total_win_rate": t_line,
            "total_mean": total_stats["mean"],
            "active_count": len(active),
            "delisted_count": len(delisted),
            "total_count": len(all_returns),
        })

    report_sections["technical_signals"] = "\n".join(report_lines)

    # --- 信号排名 ---
    if signal_ranking:
        print(f"\n\n🏆 技术信号排名（按整体5日胜率降序）:")
        buy_signals = [s for s in signal_ranking if "sell" in s["type"] or "death" in s["type"] or "breakdown" in s["type"] or "panic" in s["type"]]
        sell_signals = [s for s in signal_ranking if not ("sell" in s["type"] or "death" in s["type"] or "breakdown" in s["type"] or "panic" in s["type"])]

        # 买入信号按胜率排序
        print(f"\n  ✅ 买入信号排名:")
        buy_sorted = sorted(sell_signals, key=lambda s: float(s["total_win_rate"].replace("%", "")), reverse=True)
        rank_lines = ["| 排名 | 买入信号 | 整体胜率 | 均收益 | 活跃股胜率 | 退市股胜率 | 样本量 |"]
        rank_lines.append("|:---:|:--------|:-------:|:-----:|:---------:|:---------:|:-----:|")
        for rank, s in enumerate(buy_sorted[:8], 1):
            wr = s["total_win_rate"]
            print(f"    {rank}. {s['name']:<35} | 胜率{wr} | 均收益{s['total_mean']:+.2f}% | 活跃{s['active_win_rate']:.0f}% | 退市{s['delisted_win_rate']:.0f}% | n={s['total_count']}")
            rank_lines.append(f"| {rank} | {s['name']} | {wr} | {s['total_mean']:+.2f}% | {s['active_win_rate']:.1f}% | {s['delisted_win_rate']:.1f}% | {s['total_count']} |")
        report_sections["buy_signal_ranking"] = "\n".join(rank_lines)

        # 卖出信号按正确率排序
        print(f"\n  🔴 卖出信号排名（卖出后5日下跌=正确）:")
        sell_sorted = sorted(buy_signals, key=lambda s: s["active_accuracy"] if s["active_count"] > 0 else 0, reverse=True)
        rank_lines2 = ["| 排名 | 卖出信号 | 整体正确率 | 均收益(负=好) | 活跃股正确率 | 退市股正确率 | 样本量 |"]
        rank_lines2.append("|:---:|:--------|:---------:|:-----------:|:-----------:|:-----------:|:-----:|")
        for rank, s in enumerate(sell_sorted[:8], 1):
            print(f"    {rank}. {s['name']:<35} | 整体正确率{s['total_win_rate']} | 均收益{s['total_mean']:+.2f}% | 活跃{s['active_accuracy']:.0f}% | 退市{s['delisted_accuracy']:.0f}% | n={s['total_count']}")
            rank_lines2.append(f"| {rank} | {s['name']} | {s['total_win_rate']} | {s['total_mean']:+.2f}% | {s['active_accuracy']:.1f}% | {s['delisted_accuracy']:.1f}% | {s['total_count']} |")
        report_sections["sell_signal_ranking"] = "\n".join(rank_lines2)

    # --- 交易规则表现 ---
    print(f"\n{'=' * 60}")
    print("💰 交易执行规则回测结果")
    print("=" * 60)

    rule_name_map = {
        "no_stop": "不止损（持有20日）",
        "fixed_stop_7": "固定止损-7%",
        "take_profit_15_20": "分批止盈+15%/+20%",
        "trailing_stop": "移动止盈(盈利>10%后回撤-5%)",
        "time_stop_5d": "时间止损(5日不达预期减半)",
    }

    print(f"\n{'规则':<40} | {'活跃股':>19} | {'退市股':>19} | {'整体':>19}")
    print("-" * 100)
    print(f"{'':40} | {'均收益':>8} {'胜率':>8} | {'均收益':>8} {'胜率':>8} | {'均收益':>8} {'胜率':>8}")
    print("-" * 100)

    rule_lines = ["| 交易规则 | 活跃股均收益 | 活跃股胜率 | 退市股均收益 | 退市股胜率 | 整体均收益 | 整体胜率 |"]
    rule_lines.append("|:--------|:----------:|:---------:|:----------:|:---------:|:---------:|:-------:|")

    for rule_name, data in sorted(trade_rule_results.items()):
        active_ret = data.get("active", [])
        delisted_ret = data.get("delisted", [])
        all_ret = active_ret + delisted_ret

        a_s = _summarize_returns(active_ret)
        d_s = _summarize_returns(delisted_ret)
        t_s = _summarize_returns(all_ret)

        label = rule_name_map.get(rule_name, rule_name)
        print(f"  {label:<38} | {a_s['mean']:>+7.2f}% {a_s['win_rate']:>6.1f}% | {d_s['mean']:>+7.2f}% {d_s['win_rate']:>6.1f}% | {t_s['mean']:>+7.2f}% {t_s['win_rate']:>6.1f}%")
        rule_lines.append(f"| {label} | {a_s['mean']:+.2f}% | {a_s['win_rate']:.1f}% | {d_s['mean']:+.2f}% | {d_s['win_rate']:.1f}% | {t_s['mean']:+.2f}% | {t_s['win_rate']:.1f}% |")

    report_sections["trading_rules"] = "\n".join(rule_lines)

    # 交易规则的幸存者偏差分析
    print(f"\n\n  💡 幸存者偏差分析:")
    print(f"  {'规则':<38} | {'活跃-退市均收益差':>18} | {'活跃-退市胜率差':>18}")
    print("  " + "-" * 78)
    bias_lines = ["| 规则 | 活跃-退市均收益差 | 活跃-退市胜率差 |"]
    bias_lines.append("|:----|:---------------:|:---------------:|")
    for rule_name, data in sorted(trade_rule_results.items()):
        active_ret = data.get("active", [])
        delisted_ret = data.get("delisted", [])
        a_s = _summarize_returns(active_ret)
        d_s = _summarize_returns(delisted_ret)
        diff_mean = a_s["mean"] - d_s["mean"]
        diff_wr = a_s["win_rate"] - d_s["win_rate"]
        label = rule_name_map.get(rule_name, rule_name)
        print(f"  {label:<38} | {diff_mean:>+8.2f}% {'🟢' if abs(diff_mean) < 5 else '🔴':>8} | {diff_wr:>+7.1f}% {'🟢' if abs(diff_wr) < 10 else '🔴':>8}")
        bias_lines.append(f"| {label} | {diff_mean:+.2f}% | {diff_wr:+.1f}% |")
    report_sections["survivorship_bias"] = "\n".join(bias_lines)

    # === 综合分析报告 ===
    report = _generate_report(report_sections, signal_ranking, trade_rule_results, processed, errors)

    return report


def _generate_report(sections: dict, signal_ranking: list,
                     trade_rule_results: dict, total_processed: int, total_errors: int) -> str:
    """生成完整的Markdown分析报告"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    today = datetime.now().strftime("%Y-%m-%d")

    report = f"""# 全策略含退市股回测报告

> 生成时间：{now}
> 回测引擎：`scripts/utils/research_all_strategies.py`
> 回测包含：活跃股（含退市股数据说明见下方）

---

## 一、幸存者偏差总览

本报告所有策略信号均使用**活跃股 + 退市股**双重回测，以量化幸存者偏差的影响。

| 偏差类型 | 说明 |
|:---------|:-----|
| 🔴 胜率偏差 | 退市股在被淘汰前通常持续走弱，策略信号频繁触发但表现差 |
| 🔴 均收益偏差 | 退市股的尾部风险（-70%/-90%归零）拉低整体均收益 |
| 🔴 信号频率偏差 | 退市股在退市前产生"抄底"信号的频率远高于正常股 |
| 🟢 修正方法 | 所有信号统计同时标注"仅活跃股"和"含退市股"两组数据 |

---

## 二、技术信号回测结果

### 2.1 信号汇总

> 评估方法：买入信号看5日后收益（>0=正确），卖出信号看5日后是否下跌（<0=正确）

{sections.get('technical_signals', '（无数据）')}

### 2.2 买入信号排名

{sections.get('buy_signal_ranking', '（无数据）')}

### 2.3 卖出信号排名

{sections.get('sell_signal_ranking', '（无数据）')}

---

## 三、交易规则回测结果

### 3.1 规则表现

> 评估方法：每20个交易日模拟一次买入，持有最多20日，按规则提前退出

{sections.get('trading_rules', '（无数据）')}

### 3.2 幸存者偏差影响

> 偏差值 = 活跃股表现 - 退市股表现。正值表示活跃股优于退市股（存在幸存者偏差）

{sections.get('survivorship_bias', '（无数据）')}

---

## 四、核心结论

"""

    # 从信号排名提取Top买入信号
    buy_signals = [s for s in signal_ranking if not (s["is_sell"])]
    sell_signals = [s for s in signal_ranking if s["is_sell"]]
    buy_sorted = sorted(buy_signals, key=lambda s: float(s["total_win_rate"].replace("%", "")), reverse=True)

    if buy_sorted:
        report += f"""### ✅ 最可靠的买入信号（含退市股）:

"""
        for s in buy_sorted[:3]:
            wr = s["total_win_rate"]
            bias = float(wr.replace("%", "")) - s["delisted_win_rate"]
            report += f"1. **{s['name']}** — 胜率{wr}，活跃股胜率{s['active_win_rate']:.1f}%，退市股胜率{s['delisted_win_rate']:.1f}%（偏差{bias:+.1f}%）\n"

    if sell_signals:
        # 卖出信号按正确率排序
        sell_sorted = sorted(sell_signals, key=lambda s: s["active_accuracy"] if s["active_count"] > 0 else 0, reverse=True)
        report += f"""\n### 🔴 最可靠的卖出信号（含退市股）:

"""
        for s in sell_sorted[:3]:
            acc = s["total_win_rate"]
            bias = float(acc.replace("%", "")) - s["delisted_accuracy"]
            report += f"1. **{s['name']}** — 正确率{acc}，活跃股{s['active_accuracy']:.1f}%，退市股{s['delisted_accuracy']:.1f}%（偏差{bias:+.1f}%）\n"

    # 交易规则结论
    rule_name_map = {
        "no_stop": "不止损（持有20日）",
        "fixed_stop_7": "固定止损-7%",
        "take_profit_15_20": "分批止盈+15%/+20%",
        "trailing_stop": "移动止盈(盈利>10%后回撤-5%)",
        "time_stop_5d": "时间止损(5日不达预期减半)",
    }

    report += f"""\n### 💰 交易规则生存力分析:

"""
    for rule_name in ["no_stop", "fixed_stop_7", "take_profit_15_20", "trailing_stop", "time_stop_5d"]:
        data = trade_rule_results.get(rule_name, {})
        active_ret = data.get("active", [])
        delisted_ret = data.get("delisted", [])

        if active_ret and delisted_ret:
            a_mean = np.mean(active_ret)
            d_mean = np.mean(delisted_ret)
            diff = a_mean - d_mean

            if diff > 3:
                verdict = "⚠️ 存在显著幸存者偏差，退市股拖累严重"
            elif diff > 1:
                verdict = "🔶 轻微偏差，规则对退市股更敏感"
            else:
                verdict = "🟢 偏差可控，规则在退市股上也基本有效"

            report += f"- **{rule_name_map.get(rule_name, rule_name)}**: 活跃{diff:+.2f}% vs 退市{d_mean:+.2f}%（差{diff:+.2f}%）→ {verdict}\n"

    report += f"""

---

## 五、原始数据说明

| 指标 | 数值 |
|:----|:-----|
| 回测股票总数 | {total_processed} 只 |
| 错误数 | {total_errors} 只 |
| 数据时间范围 | 2000-01-01 至 {today} |
| 数据源 | Tushare Pro daily API |
| 回测方法 | 信号触发后统计N日收益（不考虑手续费） |

### 已知局限

1. **只考虑价格+成交量**：选股策略中的估值因子（PE/PB）、成长因子（营收/利润）、情绪因子（北向资金/龙虎榜）无法用于退市股回测（这些数据在退市前停止更新）
2. **不考虑交易成本**：未计入佣金（万2.5）、印花税（千1）、滑点
3. **不考虑市场冲击**：假设所有交易以收盘价成交
4. **信号独立评估**：未考虑多信号叠加效果
5. **时间范围限制**：退市股的历史价格数据可能存在缺口

### 需要进一步分析的
- 多信号组合的叠加效果
- MACD+RSI+BOLL三重确认的胜率
- 不同市场环境（牛市/熊市/震荡）下的策略表现分化
"""

    return report


# =====================================================================
# 入口
# =====================================================================

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="全策略含退市股回测")
    parser.add_argument("--max-stocks", type=int, default=1000,
                       help="活跃股回测数量（默认1000）")
    parser.add_argument("--sample-delisted", type=int, default=None,
                       help="退市股取样数（默认全部）")
    parser.add_argument("--force-fetch", action="store_true",
                       help="强制重新拉取退市股数据")
    parser.add_argument("--fetch-only", action="store_true",
                       help="仅拉取退市股数据，不跑回测")
    parser.add_argument("--output", type=str, default=None,
                       help="输出报告路径")
    args = parser.parse_args()

    db = DatabaseManager()

    if args.fetch_only:
        fetch_delisted_stocks_to_db(db)
        sys.exit(0)

    report = run_full_backtest(
        db,
        max_stocks=args.max_stocks,
        force_fetch=args.force_fetch,
        sample_delisted=args.sample_delisted,
    )

    # 输出报告
    output_path = args.output or os.path.join(
        os.path.dirname(__file__), "..", "..",
        "knowledge", "策略", "含退市股全策略回测报告.md"
    )

    # 写入文件
    try:
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(report)
        print(f"\n  ✅ 报告写入: {output_path}")
    except Exception as e:
        print(f"\n  ❌ 报告写入失败: {e}")

    # 更新变更记录
    try:
        from scripts.utils.knowledge_lint import update_changes
        update_changes("📝回测", output_path.replace("\\", "/").split("/")[-1], "全策略含退市股回测报告")
    except Exception as e:
        print(f"  ⚠️ 知识库更新失败（非关键错误）: {e}")

    print(f"\n{'=' * 60}")
    print(f"📄 报告已生成: {output_path}")
    print(f"{'=' * 60}")
