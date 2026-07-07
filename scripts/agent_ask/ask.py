"""
Agent策略问股系统 — 借鉴 daily_stock_analysis 的15+内置策略问股设计

支持多种策略模板的自然语言问股：
- 均线类：均线金叉、多头趋势、均线缠绕
- 理论类：缠论(笔/线段/中枢)、波浪理论(艾略特波浪)
- 量价类：量价背离、放量突破、缩量回调
- 题材类：热点题材、事件驱动
- 技术类：MACD背离、KDJ超买超卖、RSI强度、布林带
- 基本面：成长质量、估值修复

用法：
    python scripts/agent_ask/ask.py --code 600519 --strategy 缠论
    python scripts/agent_ask/ask.py --code 300750 --strategy 均线 --mode detail

D3异常处理表：
| 触发条件 | 一线修复 | 仍失败兜底 |
|---------|---------|-----------|
| 股票代码无效 | 自动补全.SH/.SZ | 提示"未找到该股票" |
| 行情数据不足(<60日) | 用可用数据计算 | 标注"数据不足，分析仅供参考" |
| 指定策略未找到 | 模糊匹配最相近策略 | 使用"综合技术分析"策略 |
| 技术指标计算失败 | 跳过该指标 | 基于可用指标做分析 |
| 实时行情不可用 | 使用最近日线数据 | 标注"非实时数据" |
"""

import sys
import os
import json
import argparse
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
import pandas as pd
import numpy as np

from scripts.utils.tushare_client import pro

# ── a-stock-data 估值数据源（优雅降级） ──
try:
    from scripts.utils.tencent_provider import tencent_quote, normalize_code
    _HAS_TENCENT = True
except Exception:
    _HAS_TENCENT = False
try:
    from scripts.utils.ths_provider import ths_eps_forecast
    _HAS_THS_EPS = True
except Exception:
    _HAS_THS_EPS = False


# ============================================================
#  数据获取
# ============================================================

def _get_daily(ts_code: str, days: int = 250) -> pd.DataFrame:
    """获取日线数据（优先DB，回退API）"""
    from scripts.utils.db_manager import DatabaseManager
    try:
        _db = DatabaseManager()
        df = _db.get_daily_price(ts_code)
        if df is not None and not df.empty and len(df) >= 10:
            df = df.sort_values("trade_date", ascending=True).reset_index(drop=True)
            return df.tail(days)
    except Exception as e:
        print(f"  [WARN] DB行情获取失败 ({ts_code}), 回退API: {e}")
    try:
        df = pro.daily(ts_code=ts_code)
        if df is not None and not df.empty:
            df = df.sort_values("trade_date", ascending=True).reset_index(drop=True)
            return df.tail(days)
    except Exception as e:
        print(f"  [WARN] API行情获取失败 ({ts_code}): {e}")
    return pd.DataFrame()


def _get_indicators(ts_code: str, df: pd.DataFrame = None) -> dict:
    """获取技术指标"""
    from scripts.utils.technical_analysis import add_all_indicators, generate_signal_summary

    if df is None:
        df = _get_daily(ts_code)
    if df.empty or len(df) < 20:
        return {"error": "数据不足"}

    try:
        df_with_idx = add_all_indicators(df.copy())
        signals = generate_signal_summary(df_with_idx)

        latest = df_with_idx.iloc[-1]
        result = {
            "close": float(latest.get("close", 0)),
            "volume": float(latest.get("volume") or latest.get("vol", 0)),
            "pct_chg": float(latest.get("pct_chg", 0)),
            "signals": signals,
        }

        # MACD（列名均为小写，由 technical_analysis.py 生成）
        # 注意：ta库使用 macd_signal（非 macd_dea），macd_diff = DIF - DEA
        for k in ["macd_diff", "macd_signal", "macd"]:
            v = latest.get(k)
            if v is not None and not (isinstance(v, float) and np.isnan(v)):
                result[k] = round(float(v), 4)

        # KDJ
        for k in ["kdj_k", "kdj_d", "kdj_j"]:
            v = latest.get(k)
            if v is not None and not (isinstance(v, float) and np.isnan(v)):
                result[k] = round(float(v), 2)

        # RSI (仅 rsi_14 由 technical_analysis.py 生成，无 rsi_6)
        for k in ["rsi_14"]:
            v = latest.get(k)
            if v is not None and not (isinstance(v, float) and np.isnan(v)):
                result[k] = round(float(v), 2)

        # BOLL
        for k in ["boll_upper", "boll_mid", "boll_lower"]:
            v = latest.get(k)
            if v is not None and not (isinstance(v, float) and np.isnan(v)):
                result[k] = round(float(v), 2)

        return result
    except Exception as e:
        return {"error": f"指标计算失败: {e}"}


def _get_stock_name(ts_code: str) -> str:
    """获取股票名称"""
    try:
        df = pro.stock_basic(ts_code=ts_code, fields="ts_code,name")
        if df is not None and not df.empty:
            return str(df.iloc[0].get("name", ts_code))
    except Exception as e:
        print(f"  [WARN] 查询股票名称失败 ({ts_code}): {e}")
    return ts_code


# ============================================================
#  策略模板
# ============================================================

class StrategyTemplate:
    """策略模板基类"""
    name = "base"
    aliases = []
    description = ""

    def analyze(self, ts_code: str, df: pd.DataFrame, indicators: dict) -> dict:
        """执行策略分析"""
        return {"conclusion": "未实现", "signals": [], "detail": ""}


class MaStrategy(StrategyTemplate):
    """均线策略 — 均线金叉/多头排列/缠绕"""
    name = "均线"
    aliases = ["均线金叉", "多头排列", "均线缠绕", "ma", "移动平均线"]
    description = "基于均线系统的趋势分析（MA5/10/20/60/120），判断多头空头排列和交叉信号"

    def analyze(self, ts_code: str, df: pd.DataFrame, indicators: dict) -> dict:
        if df.empty or len(df) < 20:
            return {"conclusion": "数据不足", "signals": [], "detail": ""}
        closes = df["close"].values
        vols = df["vol"].values if "vol" in df.columns else None

        def ma(x, n):
            return np.mean(x[:n]) if len(x) >= n else None

        ma5 = ma(closes, 5)
        ma10 = ma(closes, 10)
        ma20 = ma(closes, 20)
        ma60 = ma(closes, 60) if len(closes) >= 60 else None
        ma120 = ma(closes, 120) if len(closes) >= 120 else None

        current = closes[0]
        signals = []
        detail_parts = []

        # 均线排列判断
        if ma5 and ma10 and ma20:
            if ma5 > ma10 > ma20 and (ma60 is None or ma20 > ma60):
                trend = "多头排列 ✅"
                signals.append("strong_buy")
                detail_parts.append(f"均线多头排列(MA5={ma5:.2f}>{ma10:.2f}>{ma20:.2f})，上升趋势确认")
            elif ma5 < ma10 < ma20 and (ma60 is None or ma20 < ma60):
                trend = "空头排列 ❌"
                signals.append("strong_sell")
                detail_parts.append(f"均线空头排列(MA5={ma5:.2f}<{ma10:.2f}<{ma20:.2f})，下降趋势")
            elif ma5 > ma20:
                trend = "短期偏多 📈"
                signals.append("buy")
                detail_parts.append(f"短期均线在MA20上方(MA5={ma5:.2f}>MA20={ma20:.2f})")
            else:
                trend = "震荡整理 🔄"
                detail_parts.append(f"均线缠绕(MA5={ma5:.2f},MA10={ma10:.2f},MA20={ma20:.2f})，方向不明")
        else:
            trend = "数据不足"
        result = {"trend": trend, "ma5": ma5, "ma10": ma10, "ma20": ma20}

        # 金叉/死叉判断
        if len(closes) >= 11:
            prev_ma5 = ma(closes[1:], 5)
            prev_ma10 = ma(closes[1:], 10)
            if prev_ma5 and prev_ma10 and ma5 and ma10:
                if prev_ma5 <= prev_ma10 and ma5 > ma10:
                    signals.append("buy")
                    detail_parts.append(f"MA5上穿MA10形成金叉 ⭐")
                elif prev_ma5 >= prev_ma10 and ma5 < ma10:
                    signals.append("sell")
                    detail_parts.append(f"MA5下穿MA10形成死叉 ⚠️")

        # 价格与均线位置
        if ma20 and current:
            pct_off_ma20 = (current - ma20) / ma20 * 100
            result["偏离MA20"] = f"{pct_off_ma20:.1f}%"
            if abs(pct_off_ma20) > 8:
                detail_parts.append(f"价格偏离MA20达{pct_off_ma20:.1f}%，乖离率偏大")
                signals.append("warning")

        # 成交量验证
        if vols is not None and len(vols) >= 10:
            avg_vol = np.mean(vols[:10])
            recent_vol = np.mean(vols[:3])
            if avg_vol > 0 and recent_vol > avg_vol * 1.5:
                detail_parts.append(f"放量({recent_vol/avg_vol:.1f}x)")
                if "buy" in signals or "strong_buy" in signals:
                    signals.append("volume_confirmed")

        return {
            "conclusion": trend,
            "signals": signals,
            "detail": "\n".join(detail_parts),
            "data": result,
        }


class ChanTheoryStrategy(StrategyTemplate):
    """缠论策略 — 笔/线段/中枢识别"""
    name = "缠论"
    aliases = ["缠中说禅", "chan", "禅论"]
    description = "基于缠论的笔、线段、中枢识别，判断买卖点"

    def analyze(self, ts_code: str, df: pd.DataFrame, indicators: dict) -> dict:
        if df.empty or len(df) < 60:
            return {"conclusion": "数据不足（需至少60个交易日）", "signals": [], "detail": ""}
        closes = df["close"].values
        highs = df["high"].values if "high" in df.columns else closes
        lows = df["low"].values if "low" in df.columns else closes

        # 简化缠论：顶底分型识别
        tops, bottoms = [], []
        for i in range(2, len(closes) - 2):
            # 顶分型: 中间高点 > 左右
            if highs[i] > highs[i-1] and highs[i] > highs[i+1] and highs[i] >= highs[i-2] and highs[i] >= highs[i+2]:
                tops.append((i, highs[i]))
            # 底分型: 中间低点 < 左右
            if lows[i] < lows[i-1] and lows[i] < lows[i+1] and lows[i] <= lows[i-2] and lows[i] <= lows[i+2]:
                bottoms.append((i, lows[i]))

        # 笔识别（最近3笔）
        recent_tops = tops[-5:] if len(tops) >= 5 else tops
        recent_bottoms = bottoms[-5:] if len(bottoms) >= 5 else bottoms

        # 当前笔方向
        if recent_tops and recent_bottoms:
            last_top_idx = recent_tops[-1][0]
            last_bottom_idx = recent_bottoms[-1][0]
            if last_top_idx > last_bottom_idx:
                current_bi = "向下笔（从顶到底）"
            else:
                current_bi = "向上笔（从底到顶）"
        else:
            current_bi = "数据不足"

        # 中枢识别（简化：3笔重叠区域）
        signals = []
        detail_parts = [f"识别到{len(tops)}个顶分型, {len(bottoms)}个底分型"]
        detail_parts.append(f"当前笔方向: {current_bi}")

        if recent_tops and recent_bottoms:
            recent_tops_prices = [t[1] for t in recent_tops]
            recent_bottoms_prices = [b[1] for b in recent_bottoms]
            if recent_tops_prices and recent_bottoms_prices:
                avg_top = np.mean(recent_tops_prices)
                avg_bottom = np.mean(recent_bottoms_prices)
                if avg_top > avg_bottom:
                    range_pct = (avg_top - avg_bottom) / avg_bottom * 100
                    detail_parts.append(f"近期波动区间: {avg_bottom:.2f} - {avg_top:.2f} ({range_pct:.1f}%)")
                    if current_bi == "向上笔（从底到顶）":
                        signals.append("buy")
                        detail_parts.append("向上笔延伸中，持有关注")
                    else:
                        signals.append("watch")
                        detail_parts.append("向下笔延伸中，等待底分型确认")
                else:
                    detail_parts.append("价格重心下移")

        # 背驰判断（简化：用MACD顶底背离）
        if indicators and "macd_diff" in indicators:
            diff = indicators["macd_diff"]
            if "close" in indicators:
                price = indicators["close"]
                if signals and signals[-1] == "buy" and diff > 0:
                    detail_parts.append(f"MACD为正({diff:.2f})，向上笔有力")
                elif signals and signals[-1] == "watch" and diff < 0:
                    detail_parts.append(f"MACD为负({diff:.2f})，向下笔延续")

        conclusion = ", ".join(detail_parts[:2])
        return {
            "conclusion": current_bi,
            "signals": signals,
            "detail": "\n".join(detail_parts),
            "data": {"tops": len(tops), "bottoms": len(bottoms), "last_bi": current_bi},
        }


class WaveTheoryStrategy(StrategyTemplate):
    """波浪理论策略 — 艾略特波浪识别"""
    name = "波浪理论"
    aliases = ["波浪", "elliott", "艾略特波浪"]
    description = "基于艾略特波浪理论的五浪推动+三浪调整识别"

    def analyze(self, ts_code: str, df: pd.DataFrame, indicators: dict) -> dict:
        if df.empty or len(df) < 100:
            return {"conclusion": "数据不足（需至少100个交易日）", "signals": [], "detail": ""}
        closes = df["close"].values

        # 简化波浪识别：寻找显著的波段高点/低点
        # 用5日均线的转折点近似波浪
        ma5_vals = pd.Series(closes).rolling(5).mean().dropna().values
        if len(ma5_vals) < 20:
            return {"conclusion": "数据不足", "signals": [], "detail": ""}

        # 找转折点
        turning_points = []
        for i in range(2, len(ma5_vals) - 2):
            if ma5_vals[i] > ma5_vals[i-1] and ma5_vals[i] > ma5_vals[i+1]:
                turning_points.append(("顶", i, ma5_vals[i]))
            elif ma5_vals[i] < ma5_vals[i-1] and ma5_vals[i] < ma5_vals[i+1]:
                turning_points.append(("底", i, ma5_vals[i]))

        recent = turning_points[-8:] if len(turning_points) >= 8 else turning_points
        signals = []
        detail_parts = [f"识别到{len(recent)}个波段转折点"]

        if len(recent) >= 5:
            # 检查是否为推动浪（高-高，低-低）
            wave_counts = {"impulse": 0, "corrective": 0}
            for j in range(1, len(recent)):
                if recent[j][0] != recent[j-1][0]:
                    if recent[j][2] > recent[j-1][2]:
                        wave_counts["impulse"] += 1
                    else:
                        wave_counts["corrective"] += 1

            detail_parts.append(f"推动浪{wave_counts['impulse']}波，调整浪{wave_counts['corrective']}波")
            if wave_counts["impulse"] >= 3:
                detail_parts.append("疑似主升浪阶段 ⭐")
                signals.append("buy")
            elif wave_counts["corrective"] >= 2:
                detail_parts.append("调整浪阶段，等待推动浪启动")
                signals.append("watch")
        else:
            detail_parts.append("波段过少，无法判断波浪结构")

        return {
            "conclusion": detail_parts[-1] if detail_parts else "分析完成",
            "signals": signals,
            "detail": "\n".join(detail_parts),
            "data": {"waves_detected": len(recent)},
        }


class MacdDivergenceStrategy(StrategyTemplate):
    """MACD背离策略"""
    name = "MACD背离"
    aliases = ["macd", "macd背离", "顶背离", "底背离"]
    description = "基于MACD的顶背离/底背离识别"

    def analyze(self, ts_code: str, df: pd.DataFrame, indicators: dict) -> dict:
        if df.empty or len(df) < 60:
            return {"conclusion": "数据不足", "signals": [], "detail": ""}
        closes = df["close"].values

        # 使用已计算的MACD
        try:
            from scripts.utils.technical_analysis import add_all_indicators
            df_idx = add_all_indicators(df.copy())
            if df_idx.empty:
                return {"conclusion": "MACD计算失败", "signals": [], "detail": ""}

            diff_vals = df_idx["macd_diff"].dropna().values
            close_vals = df_idx["close"].dropna().values

            if len(diff_vals) < 20 or len(close_vals) < 20:
                return {"conclusion": "MACD数据不足", "signals": [], "detail": ""}

            # 顶背离: 价格创新高，MACD未创新高
            recent_close = close_vals[-15:]
            recent_diff = diff_vals[-15:]

            price_peak = max(recent_close)
            price_peak_idx = list(recent_close).index(price_peak)
            diff_peak = max(recent_diff)

            signals = []
            detail_parts = []

            # 顶背离检查
            if price_peak == recent_close[-1] or price_peak == recent_close[-2]:  # 最近创新高
                # 检查MACD是否同步创新高
                diff_at_price_peak = recent_diff[price_peak_idx]
                if diff_at_price_peak < diff_peak * 0.9:  # MACD明显低于前期高点
                    signals.append("bearish_divergence")
                    detail_parts.append(f"⚠️ 顶背离! 价格创新高({price_peak:.2f})但MACD未同步({diff_at_price_peak:.2f}<{diff_peak:.2f})")

            # 底背离检查
            price_trough = min(recent_close)
            price_trough_idx = list(recent_close).index(price_trough)
            diff_trough = min(recent_diff)

            if price_trough == recent_close[-1] or price_trough == recent_close[-2]:
                diff_at_price_trough = recent_diff[price_trough_idx]
                if diff_at_price_trough > diff_trough * 0.9:
                    signals.append("bullish_divergence")
                    detail_parts.append(f"⭐ 底背离! 价格创新低({price_trough:.2f})但MACD未同步({diff_at_price_trough:.2f}>{diff_trough:.2f})")

            if not detail_parts:
                detail_parts.append("MACD与价格走势一致，无明显背离")

            return {
                "conclusion": detail_parts[0],
                "signals": signals,
                "detail": "\n".join(detail_parts),
                "data": {"diff_current": round(diff_vals[-1], 4) if len(diff_vals) > 0 else None},
            }
        except Exception as e:
            return {"conclusion": f"分析失败: {e}", "signals": [], "detail": ""}


class VolumePriceStrategy(StrategyTemplate):
    """量价分析策略 — 量价背离/放量突破/缩量回调"""
    name = "量价分析"
    aliases = ["量价", "量价背离", "放量突破", "缩量回调", "成交量"]
    description = "基于成交量与价格关系的分析"

    def analyze(self, ts_code: str, df: pd.DataFrame, indicators: dict) -> dict:
        if df.empty or len(df) < 20:
            return {"conclusion": "数据不足", "signals": [], "detail": ""}
        closes = df["close"].values
        volumes = df["vol"].values if "vol" in df.columns else None
        if volumes is None:
            return {"conclusion": "无成交量数据", "signals": [], "detail": ""}

        signals = []
        detail_parts = []

        # 均量
        avg_vol_20 = np.mean(volumes[:20])
        avg_vol_5 = np.mean(volumes[:5])
        recent_vol = np.mean(volumes[:3])

        # 价格趋势
        ret_5d = (closes[0] - closes[min(4, len(closes)-1)]) / closes[min(4, len(closes)-1)] * 100 if len(closes) >= 5 else 0

        vol_ratio = recent_vol / avg_vol_20 if avg_vol_20 > 0 else 1

        # 放量上涨
        if vol_ratio > 1.5 and ret_5d > 3:
            signals.append("strong_buy")
            detail_parts.append(f"⭐ 放量上涨(量比{vol_ratio:.1f}x,涨幅{ret_5d:.1f}%)，强势突破信号")
        # 放量下跌
        elif vol_ratio > 1.5 and ret_5d < -3:
            signals.append("strong_sell")
            detail_parts.append(f"⚠️ 放量下跌(量比{vol_ratio:.1f}x,跌幅{ret_5d:.1f}%)，资金出逃")
        # 缩量回调
        elif vol_ratio < 0.7 and ret_5d < -2:
            signals.append("buy")
            detail_parts.append(f"缩量回调(量比{vol_ratio:.1f}x,跌幅{ret_5d:.1f}%)，洗盘特征")
        # 缩量上涨
        elif vol_ratio < 0.7 and ret_5d > 2:
            signals.append("watch")
            detail_parts.append(f"缩量上涨(量比{vol_ratio:.1f}x,涨幅{ret_5d:.1f}%)，上涨动能不足")
        else:
            detail_parts.append(f"量价正常(量比{vol_ratio:.1f}x,近5日涨幅{ret_5d:.1f}%)")

        # 天量天价/地量地价
        max_vol_idx = np.argmax(volumes[:20])
        if max_vol_idx == 0 and vol_ratio > 2:
            detail_parts.append("⚠️ 出现天量，警惕见顶风险")
            signals.append("warning")

        return {
            "conclusion": detail_parts[0] if detail_parts else "量价分析完成",
            "signals": signals,
            "detail": "\n".join(detail_parts),
            "data": {"vol_ratio": round(vol_ratio, 2), "ret_5d": round(ret_5d, 2)},
        }


class RsiStrategy(StrategyTemplate):
    """RSI强度策略"""
    name = "RSI强度"
    aliases = ["rsi", "相对强度"]
    description = "基于RSI指标的超买超卖分析"

    def analyze(self, ts_code: str, df: pd.DataFrame, indicators: dict) -> dict:
        if not indicators or "rsi_14" not in indicators:
            return {"conclusion": "RSI数据不足", "signals": [], "detail": ""}
        rsi14 = indicators["rsi_14"]

        signals = []
        detail_parts = [f"RSI(14)={rsi14}"]

        if rsi14 > 80:
            signals.append("overbought")
            detail_parts.append("⚠️ RSI>80，超买区域，注意回调风险")
        elif rsi14 > 70:
            signals.append("strong")
            detail_parts.append("RSI在70-80之间，强势区间")
        elif rsi14 > 50:
            signals.append("bullish")
            detail_parts.append("RSI>50，多头占优")
        elif rsi14 > 30:
            signals.append("bearish")
            detail_parts.append("RSI<50，空头占优")
        else:
            signals.append("oversold")
            detail_parts.append("⭐ RSI<30，超卖区域，关注反弹机会")

        # RSI趋势判断（对比中性值50）
        if rsi14 > 50:
            detail_parts.append("RSI>50，整体偏强")
        else:
            detail_parts.append("RSI≤50，整体偏弱")

        return {
            "conclusion": detail_parts[-1] if len(detail_parts) > 1 else detail_parts[0],
            "signals": signals,
            "detail": "\n".join(detail_parts),
            "data": {"rsi_14": rsi14},
        }


class BollingerStrategy(StrategyTemplate):
    """布林带策略"""
    name = "布林带"
    aliases = ["boll", "布林", "布林线"]
    description = "基于布林带的上中下轨分析"

    def analyze(self, ts_code: str, df: pd.DataFrame, indicators: dict) -> dict:
        if not indicators or "boll_upper" not in indicators:
            return {"conclusion": "布林带数据不足", "signals": [], "detail": ""}
        upper = indicators["boll_upper"]
        middle = indicators["boll_mid"]
        lower = indicators["boll_lower"]
        close = indicators.get("close", 0)

        signals = []
        detail_parts = [f"上轨={upper:.2f}, 中轨={middle:.2f}, 下轨={lower:.2f}, 现价={close:.2f}"]

        if close >= upper:
            signals.append("overbought")
            detail_parts.append("⚠️ 价格触及上轨，超买")
        elif close <= lower:
            signals.append("oversold")
            detail_parts.append("⭐ 价格触及下轨，超卖")
        elif close > middle:
            signals.append("bullish")
            detail_parts.append("价格在中轨上方，偏强")
        else:
            signals.append("bearish")
            detail_parts.append("价格在中轨下方，偏弱")

        # 带宽
        bandwidth = (upper - lower) / middle * 100 if middle > 0 else 0
        detail_parts.append(f"带宽={bandwidth:.1f}%")
        if bandwidth < 5:
            detail_parts.append("布林带极度收窄，即将变盘 ⚡")

        return {
            "conclusion": detail_parts[-2] if len(detail_parts) > 2 else detail_parts[0],
            "signals": signals,
            "detail": "\n".join(detail_parts),
            "data": {"upper": upper, "middle": middle, "lower": lower, "bandwidth": round(bandwidth, 1)},
        }


class KdjStrategy(StrategyTemplate):
    """KDJ策略"""
    name = "KDJ"
    aliases = ["kdj", "随机指标"]
    description = "基于KDJ的超买超卖和金叉死叉分析"

    def analyze(self, ts_code: str, df: pd.DataFrame, indicators: dict) -> dict:
        if not indicators or "kdj_k" not in indicators:
            return {"conclusion": "KDJ数据不足", "signals": [], "detail": ""}
        k, d, j = indicators["kdj_k"], indicators["kdj_d"], indicators["kdj_j"]

        signals = []
        detail_parts = [f"K={k:.1f}, D={d:.1f}, J={j:.1f}"]

        if j > 100:
            signals.append("overbought")
            detail_parts.append("⚠️ J>100，超买")
        elif j < 0:
            signals.append("oversold")
            detail_parts.append("⭐ J<0，超卖")
        elif k > 80:
            detail_parts.append("K>80，偏强")
        elif k < 20:
            detail_parts.append("K<20，偏弱")

        # 金叉死叉
        if k > d and j > k:
            signals.append("buy")
            detail_parts.append("K>D>J, 多头排列")
        elif k < d and j < k:
            signals.append("sell")
            detail_parts.append("K<D<J, 空头排列")

        return {
            "conclusion": detail_parts[0] if len(detail_parts) > 0 else "KDJ分析完成",
            "signals": signals,
            "detail": "\n".join(detail_parts),
            "data": {"k": k, "d": d, "j": j},
        }


class ComprehensiveStrategy(StrategyTemplate):
    """综合技术分析 — 默认策略"""
    name = "综合"
    aliases = ["综合技术分析", "技术面", "全面分析", "default"]
    description = "综合多种技术指标的综合分析"

    def analyze(self, ts_code: str, df: pd.DataFrame, indicators: dict) -> dict:
        parts = []
        all_signals = []

        # 运行全部8个子策略
        for s in [MaStrategy(), ChanTheoryStrategy(), WaveTheoryStrategy(),
                  MacdDivergenceStrategy(), VolumePriceStrategy(),
                  RsiStrategy(), BollingerStrategy(), KdjStrategy()]:
            try:
                r = s.analyze(ts_code, df, indicators)
                if r.get("detail"):
                    parts.append(f"【{s.name}】\n{r['detail']}")
                all_signals.extend(r.get("signals", []))
            except Exception as e:
                print(f"  [WARN] 子策略{s.name}分析失败 ({ts_code}): {e}")

        # 综合判断
        buy_signals = sum(1 for sig in all_signals if sig in ("buy", "strong_buy", "bullish_divergence"))
        sell_signals = sum(1 for sig in all_signals if sig in ("sell", "strong_sell", "bearish_divergence"))

        if buy_signals > sell_signals + 1:
            conclusion = f"📈 综合偏多 (多{buy_signals}个/空{sell_signals}个信号)"
        elif sell_signals > buy_signals + 1:
            conclusion = f"📉 综合偏空 (空{sell_signals}个/多{buy_signals}个信号)"
        else:
            conclusion = f"🔄 综合震荡 (多{buy_signals}个/空{sell_signals}个信号)"

        return {
            "conclusion": conclusion,
            "signals": all_signals,
            "detail": "\n\n".join(parts),
            "data": {"bullish": buy_signals, "bearish": sell_signals},
        }


# 策略注册表
STRATEGIES = {}
for _s_cls in [
    MaStrategy, ChanTheoryStrategy, WaveTheoryStrategy,
    MacdDivergenceStrategy, VolumePriceStrategy,
    RsiStrategy, BollingerStrategy, KdjStrategy,
    ComprehensiveStrategy,
]:
    _s_inst = _s_cls()
    STRATEGIES[_s_inst.name] = _s_inst

# 别名映射
_ALIAS_MAP = {}
for s in STRATEGIES.values():
    for alias in s.aliases:
        _ALIAS_MAP[alias] = s.name
    _ALIAS_MAP[s.name] = s.name


def resolve_strategy(name: str) -> StrategyTemplate:
    """按名称或别名查找策略"""
    if not name:
        return STRATEGIES.get("综合", ComprehensiveStrategy())
    normalized = name.strip().lower()
    # 直接匹配
    for s in STRATEGIES.values():
        if s.name == normalized or s.name == name:
            return s
    # 别名匹配
    if normalized in _ALIAS_MAP:
        return STRATEGIES[_ALIAS_MAP[normalized]]
    # 模糊匹配
    for s in STRATEGIES.values():
        if normalized in s.name.lower() or normalized in [a.lower() for a in s.aliases]:
            return s
    return STRATEGIES.get("综合", ComprehensiveStrategy())


# ============================================================
#  主分析函数
# ============================================================

def ask(code: str, strategy_name: str = "综合", mode: str = "standard") -> dict:
    """
    执行策略问股

    Args:
        code: 股票代码（如 600519, 600519.SH, 茅台）
        strategy_name: 策略名称（均线/缠论/波浪等）
        mode: standard / detail / brief

    Returns:
        分析结果dict
    """
    # 代码补全
    ts_code = code.upper()
    if not ts_code.endswith(".SH") and not ts_code.endswith(".SZ"):
        # 自动补全
        if ts_code.startswith("6"):
            ts_code = ts_code + ".SH"
        else:
            ts_code = ts_code + ".SZ"

    # ── 数据新鲜度校验：实时行情 vs DB收盘 ──
    try:
        if _HAS_TENCENT:
            from scripts.utils.tencent_provider import tencent_quote
            raw_code = code.strip().upper()
            if raw_code.endswith('.SH') or raw_code.endswith('.SZ') or raw_code.endswith('.BJ'):
                raw_code = raw_code[:-3]
            q = tencent_quote([raw_code])
            real_price = q.get(raw_code, {}).get('price', 0)
            # 用 DatabaseManager 读取 DB 最新收盘价
            from scripts.utils.db_manager import DatabaseManager
            _db_fresh = DatabaseManager()
            df_price = _db_fresh.get_daily_price(ts_code)
            # get_daily_price 按 trade_date ASC 排列，取最后一行即最新收盘
            db_price = df_price.iloc[-1]['close'] if not df_price.empty else 0
            if real_price and db_price and abs(real_price - db_price) > 0.01:
                diff_pct = (real_price - db_price) / db_price * 100
                if abs(diff_pct) > 0.5:
                    print(f"  ⚠️ 数据新鲜度警告: 实时价{real_price:.2f} vs DB收盘{db_price:.2f}"
                          f" (差异{diff_pct:+.1f}%)", file=sys.stderr)
                    print(f"     建议运行: python scripts/utils/auto_sync.py --auto-sync", file=sys.stderr)
    except Exception as e:
        print(f"  [WARN] 数据新鲜度校验失败: {e}", file=sys.stderr)

    # 获取股票名称
    stock_name = _get_stock_name(ts_code)

    # 获取数据
    df = _get_daily(ts_code)
    if df.empty:
        return {"error": f"未找到股票 {code} 的行情数据", "ts_code": ts_code, "name": stock_name}

    indicators = _get_indicators(ts_code, df)

    # 策略选择
    strategy = resolve_strategy(strategy_name)
    if not strategy:
        return {"error": f"未找到策略 '{strategy_name}'", "available": list(STRATEGIES.keys())}

    # 执行分析
    result = strategy.analyze(ts_code, df, indicators)

    # ── 实时估值数据（a-stock-data 接入） ──
    valuation = {}
    if _HAS_TENCENT:
        try:
            raw_code = normalize_code(ts_code)
            q = tencent_quote([raw_code])
            if raw_code in q:
                vi = q[raw_code]
                valuation = {
                    "pe_ttm": vi.get("pe_ttm"),
                    "pb": vi.get("pb"),
                    "mcap_yi": vi.get("mcap_yi"),
                    "float_mcap_yi": vi.get("float_mcap_yi"),
                    "turnover_pct": vi.get("turnover_pct"),
                    "limit_up": vi.get("limit_up"),
                    "limit_down": vi.get("limit_down"),
                    "high": vi.get("high"),
                    "low": vi.get("low"),
                    "amount_wan": vi.get("amount_wan"),
                }
        except Exception as e:
            print(f"  [WARN] 实时估值获取失败: {e}", file=sys.stderr)

    # ── 一致预期EPS（a-stock-data 接入） ──
    eps_forecast = {}
    if _HAS_THS_EPS:
        try:
            eps_df = ths_eps_forecast(normalize_code(ts_code))
            if not eps_df.empty and len(eps_df.columns) >= 4:
                rows_list = []
                for _, row in eps_df.iterrows():
                    row_dict = {}
                    for col in eps_df.columns:
                        try:
                            row_dict[str(col)] = float(row[col]) if row[col] is not None else None
                        except (ValueError, TypeError):
                            row_dict[str(col)] = str(row[col]) if row[col] is not None else None
                    rows_list.append(row_dict)
                eps_forecast = {
                    "data": rows_list,
                    "latest_eps": rows_list[0].get("均值") if rows_list else None,
                    "analyst_count": rows_list[0].get("预测机构数") if rows_list else 0,
                }
        except Exception as e:
            print(f"  [WARN] 一致预期EPS获取失败: {e}", file=sys.stderr)

    # 构建输出
    output = {
        "ts_code": ts_code,
        "name": stock_name,
        "strategy": strategy.name,
        "strategy_desc": strategy.description,
        "data_period": f"{df.iloc[0].get('trade_date', '?')} ~ {df.iloc[-1].get('trade_date', '?')}" if len(df) >= 2 else "N/A",
        "data_days": len(df),
        "current_price": indicators.get("close") if isinstance(indicators, dict) else None,
        "pct_chg": indicators.get("pct_chg") if isinstance(indicators, dict) else None,
        "conclusion": result.get("conclusion", ""),
        "signals": result.get("signals", []),
        "detail": result.get("detail", ""),
        "mode": mode,
        # a-stock-data 估值数据
        "valuation": valuation if valuation else None,
        "eps_forecast": eps_forecast if eps_forecast else None,
    }

    if mode == "brief":
        output.pop("detail", None)
        output.pop("data_period", None)
        output.pop("data_days", None)

    if mode == "detail" and result.get("data"):
        output["analysis_data"] = result["data"]

    return output


def format_result(result: dict) -> str:
    """格式化输出为可读文本"""
    if "error" in result:
        return f"❌ {result['error']}"

    lines = [
        f"{'='*50}",
        f"  {result.get('name', '?')}({result.get('ts_code', '?')})",
        f"  {'='*50}",
        f"  策略: {result.get('strategy', '?')}",
        f"  周期: {result.get('data_period', '?')} ({result.get('data_days', 0)}日)",
        f"  现价: {result.get('current_price', '?')}",
        f"  涨跌: {result.get('pct_chg', '?')}%",
        f"",
        f"  📋 结论: {result.get('conclusion', '?')}",
    ]

    if result.get("signals"):
        sig_map = {
            "strong_buy": "⭐ 强烈买入",
            "buy": "📈 买入",
            "watch": "👀 关注",
            "sell": "📉 卖出",
            "strong_sell": "❌ 强烈卖出",
            "warning": "⚠️ 警告",
            "overbought": "⚠️ 超买",
            "oversold": "⭐ 超卖",
            "bullish_divergence": "⭐ 底背离(看多)",
            "bearish_divergence": "⚠️ 顶背离(看空)",
            "volume_confirmed": "✅ 量能确认",
        }
        signals_str = ", ".join([sig_map.get(s, s) for s in result["signals"]])
        lines.append(f"  信号: {signals_str}")

    # ── 实时估值（a-stock-data 接入） ──
    val = result.get("valuation")
    if val and val.get("pe_ttm"):
        lines.extend([
            "",
            f"  📊 实时估值:",
            f"    PE(TTM): {val.get('pe_ttm', '?')}x | PB: {val.get('pb', '?')}x",
            f"    总市值: {val.get('mcap_yi', '?')}亿 | 换手率: {val.get('turnover_pct', '?')}%",
            f"    涨停: {val.get('limit_up', '?')} | 跌停: {val.get('limit_down', '?')}",
        ])

    # ── 一致预期EPS（a-stock-data 接入） ──
    eps = result.get("eps_forecast")
    if eps and eps.get("data"):
        lines.append("")
        lines.append("  📈 机构一致预期EPS:")
        for row in eps["data"]:
            year = row.get("年度", row.get("报告期", "?"))
            eps_val = row.get("均值", "—")
            analysts = row.get("预测机构数", "")
            lines.append(f"    {year}: EPS={eps_val} (覆盖{analysts}家)")

    if result.get("detail") and result.get("mode") != "brief":
        lines.extend(["", f"  📊 详细分析:", ""])
        for line in result["detail"].split("\n"):
            lines.append(f"    {line}")

    return "\n".join(lines)


# ============================================================
#  CLI入口
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="Agent策略问股")
    parser.add_argument("--code", "-c", required=True, help="股票代码 (如 600519)")
    parser.add_argument("--strategy", "-s", default="综合",
                        choices=list(STRATEGIES.keys()) + [a for a in _ALIAS_MAP.keys()],
                        help=f"策略模板 (可选: {', '.join(STRATEGIES.keys())})")
    parser.add_argument("--mode", "-m", default="standard", choices=["brief", "standard", "detail"],
                        help="输出模式: brief/standard/detail")
    parser.add_argument("--json", action="store_true", help="JSON格式输出")
    args = parser.parse_args()

    result = ask(args.code, args.strategy, args.mode)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    else:
        print(format_result(result))


if __name__ == "__main__":
    main()
