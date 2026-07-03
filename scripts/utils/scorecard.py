"""
L3 后置分析器 (Scorecard) — 借鉴 AlphaSift 的 L3 层设计

在 L1 因子评分 + L2 LLM排序之后，对 Top N 候选进行最终审核调整。
使用本地规则进行加减分（scorecard），无需 LLM 依赖。

用法：
    from scripts.utils.scorecard import Scorecard
    card = Scorecard()
    result = card.evaluate(ts_code, candidate_data, trade_date)
    # -> {"adjusted_score": 85, "delta": +5, "reasons": [...], "tags": [...]}

D3异常处理表：
| 触发条件 | 一线修复 | 仍失败兜底 |
|---------|---------|-----------|
| 行情数据缺失 | 跳过当日检查项 | 不调整分数 |
| 财务指标缺失 | 用前季度数据 | 跳过基本面加分项 |
| 板块数据为空 | 跳过板块关联检查 | 不调整分数 |
"""

import sys, os
import numpy as np
import pandas as pd
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from scripts.utils.tushare_client import pro


def _get_daily_price_db_first(ts_code: str, days_back: int = 60):
    """获取日线行情（DB优先）"""
    from scripts.utils.db_manager import DatabaseManager
    try:
        _db = DatabaseManager()
        df = _db.get_daily_price(ts_code)
        if df is not None and not df.empty and len(df) >= 10:
            df = df.sort_values("trade_date", ascending=False).reset_index(drop=True)
            return df.head(days_back)
    except Exception:
        pass
    try:
        df = pro.daily(ts_code=ts_code)
        if df is not None and not df.empty:
            df = df.sort_values("trade_date", ascending=False).reset_index(drop=True)
            return df.head(days_back)
    except Exception:
        pass
    return pd.DataFrame()


class ScorecardRule:
    """评分卡规则基类"""
    name = "base"

    def apply(self, ts_code: str, trade_date: str, factor_scores: dict = None) -> dict:
        """
        Returns:
            {"delta": float, "reasons": [str], "tags": [str]}
        """
        return {"delta": 0.0, "reasons": [], "tags": []}


class BreakoutConfirmRule(ScorecardRule):
    """突破确认加分：价格创新高+放量"""
    name = "突破确认"

    def apply(self, ts_code: str, trade_date: str, factor_scores: dict = None) -> dict:
        try:
            df = _get_daily_price_db_first(ts_code, days_back=30)
            if df is None or df.empty or len(df) < 20:
                return {"delta": 0, "reasons": [], "tags": []}

            closes = df["close"].values
            volumes = df["vol"].values if "vol" in df.columns else None

            if len(closes) < 10:
                return {"delta": 0, "reasons": [], "tags": []}

            current = closes[0]
            high_20d = max(closes[:20])

            # 价格接近或突破20日新高
            if current >= high_20d * 0.98:
                # 成交量确认
                if volumes is not None and len(volumes) >= 8:
                    recent_vol = np.mean(volumes[:3])
                    prev_vol = np.mean(volumes[3:8])
                    if prev_vol > 0 and recent_vol > prev_vol * 1.3:
                        return {"delta": 8, "reasons": [f"放量突破近20日新高({current:.2f}≈{high_20d:.2f})"],
                                "tags": ["突破形态"]}
                    return {"delta": 3, "reasons": [f"接近20日新高({current:.2f}/{high_20d:.2f})，量能待确认"],
                            "tags": ["接近突破"]}
            return {"delta": 0, "reasons": [], "tags": []}
        except Exception:
            return {"delta": 0, "reasons": [], "tags": []}


class VolumeConfirmRule(ScorecardRule):
    """量价配合确认：放量上涨/缩量回调"""
    name = "量价配合"

    def apply(self, ts_code: str, trade_date: str, factor_scores: dict = None) -> dict:
        try:
            df = _get_daily_price_db_first(ts_code, days_back=15)
            if df is None or df.empty or len(df) < 10:
                return {"delta": 0, "reasons": [], "tags": []}

            pct_chgs = df["pct_chg"].values
            volumes = df["vol"].values if "vol" in df.columns else None
            if volumes is None or len(volumes) < 8:
                return {"delta": 0, "reasons": [], "tags": []}

            recent_vol = np.mean(volumes[:3])
            prev_vol = np.mean(volumes[3:8])
            recent_chg = np.mean(pct_chgs[:3])

            # 放量上涨
            if recent_chg > 1 and prev_vol > 0 and recent_vol > prev_vol * 1.5:
                return {"delta": 5, "reasons": [f"放量上涨(量比{recent_vol/prev_vol:.1f}x)"],
                        "tags": ["量价齐升"]}
            # 缩量回调（健康的回调形态）
            if recent_chg < -1 and prev_vol > 0 and recent_vol < prev_vol * 0.7:
                return {"delta": 3, "reasons": [f"缩量回调(量比{recent_vol/prev_vol:.1f}x)"],
                        "tags": ["缩量回调"]}
            return {"delta": 0, "reasons": [], "tags": []}
        except Exception:
            return {"delta": 0, "reasons": [], "tags": []}


class MaArrangementRule(ScorecardRule):
    """均线排列加分：多头排列加分，空头排列减分"""
    name = "均线排列"

    def apply(self, ts_code: str, trade_date: str, factor_scores: dict = None) -> dict:
        try:
            df = _get_daily_price_db_first(ts_code, days_back=60)
            if df is None or df.empty or len(df) < 60:
                return {"delta": 0, "reasons": [], "tags": []}

            closes = df["close"].values
            if len(closes) < 60:
                return {"delta": 0, "reasons": [], "tags": []}

            ma5 = np.mean(closes[:5])
            ma10 = np.mean(closes[:10])
            ma20 = np.mean(closes[:20])
            ma60 = np.mean(closes[:60])

            if ma5 > ma10 > ma20 > ma60:
                return {"delta": 8, "reasons": [f"均线多头排列(MA5>{ma5:.1f}>{ma10:.1f}>{ma20:.1f})"],
                        "tags": ["多头排列"]}
            if ma5 < ma10 < ma20 < ma60:
                return {"delta": -8, "reasons": ["均线空头排列"], "tags": ["空头排列"]}
            if ma5 > ma20 and ma10 > ma20:
                return {"delta": 3, "reasons": ["短期均线在MA20上方"], "tags": ["短期偏多"]}
            return {"delta": 0, "reasons": [], "tags": []}
        except Exception:
            return {"delta": 0, "reasons": [], "tags": []}


class SectorMomentumRule(ScorecardRule):
    """板块联动加分：热点板块提高置信度"""
    name = "板块动量"

    def apply(self, ts_code: str, trade_date: str, factor_scores: dict = None) -> dict:
        try:
            # 获取股票行业
            df = pro.stock_basic(ts_code=ts_code, fields="ts_code,industry")
            if df is None or df.empty:
                return {"delta": 0, "reasons": [], "tags": []}
            industry = df.iloc[0].get("industry", "")
            if not industry:
                return {"delta": 0, "reasons": [], "tags": []}

            # 获取板块行情
            ths_df = pro.ths_daily(trade_date=trade_date)
            if ths_df is None or ths_df.empty:
                return {"delta": 0, "reasons": [], "tags": []}

            # 检查该行业是否在领涨板块中
            sector_names = ths_df["name"].tolist() if "name" in ths_df.columns else []
            matched = [s for s in sector_names[:10] if industry in s or s in industry]
            if matched:
                sector_pct = float(ths_df.iloc[sector_names.index(matched[0])]["pct_chg"]) if matched[0] in sector_names else 0
                if sector_pct > 3:
                    return {"delta": 5, "reasons": [f"所属板块'{matched[0]}'涨幅{sector_pct:.1f}%"],
                            "tags": ["强势板块"]}
                return {"delta": 2, "reasons": [f"所属板块'{matched[0]}'涨幅{sector_pct:.1f}%"],
                        "tags": ["板块联动"]}
            return {"delta": 0, "reasons": [], "tags": []}
        except Exception:
            return {"delta": 0, "reasons": [], "tags": []}


class FundamentalConfirmRule(ScorecardRule):
    """基本面加分：低估+高增长双重确认"""
    name = "基本面确认"

    def apply(self, ts_code: str, trade_date: str, factor_scores: dict = None) -> dict:
        try:
            basic_df = pro.daily_basic(ts_code=ts_code, trade_date=trade_date)
            if basic_df is None or basic_df.empty:
                return {"delta": 0, "reasons": [], "tags": []}
            row = basic_df.iloc[0]
            pe = row.get("pe")
            pb = row.get("pb")

            if pe is not None and pb is not None:
                # 低PE + 低PB = 双重低估
                if 0 < pe < 15 and 0 < pb < 2:
                    return {"delta": 5, "reasons": [f"双重低估(PE={pe:.1f}, PB={pb:.1f})"],
                            "tags": ["低估"]}
                # 合理PE + 合理PB
                if 15 <= pe <= 30 and 0 < pb < 5:
                    return {"delta": 2, "reasons": [f"估值合理(PE={pe:.1f}, PB={pb:.1f})"],
                            "tags": ["估值合理"]}
            return {"delta": 0, "reasons": [], "tags": []}
        except Exception:
            return {"delta": 0, "reasons": [], "tags": []}


class Scorecard:
    """
    L3 后置分析器 — 对 Top N 候选进行最终审核调整

    组合多个 ScorecardRule，对每只股票进行独立评分调整。
    借鉴 AlphaSift 的 scorecard + DSA 后置分析设计。
    """

    def __init__(self):
        self.rules = [
            BreakoutConfirmRule(),
            VolumeConfirmRule(),
            MaArrangementRule(),
            SectorMomentumRule(),
            FundamentalConfirmRule(),
        ]

    def evaluate(self, ts_code: str, trade_date: str,
                 factor_scores: dict = None, base_score: float = None) -> dict:
        """
        对单只股票执行完整评分卡评估

        Args:
            ts_code: 股票代码
            trade_date: 交易日
            factor_scores: 因子评分dict（可选，用于调试）
            base_score: 基础评分（可选，用于格式化输出）

        Returns:
            {"adjusted_score": float, "original_score": float,
             "delta": float, "reasons": [str], "tags": [str],
             "confidence": str}
        """
        total_delta = 0.0
        all_reasons = []
        all_tags = []

        for rule in self.rules:
            try:
                result = rule.apply(ts_code, trade_date, factor_scores)
                total_delta += result["delta"]
                all_reasons.extend(result["reasons"])
                all_tags.extend(result["tags"])
            except Exception as e:
                # 单个规则失败不影响其他规则
                continue

        # 置信度判断
        if total_delta > 10:
            confidence = "高"
        elif total_delta >= 3:
            confidence = "中"
        elif total_delta >= -2:
            confidence = "低"
        else:
            confidence = "不推荐"

        final_score = (base_score + total_delta) if base_score is not None else None

        return {
            "adjusted_score": round(final_score, 1) if final_score is not None else None,
            "original_score": base_score,
            "delta": round(total_delta, 1),
            "reasons": all_reasons,
            "tags": list(set(all_tags)),
            "confidence": confidence,
            "rules_applied": len([r for r in all_reasons if r]),
            "rules_total": len(self.rules),
        }

    def evaluate_batch(self, candidates: list, trade_date: str) -> list:
        """
        批量评估候选列表

        Args:
            candidates: [{"ts_code": "...", "total_score": ..., ...}, ...]
            trade_date: 交易日

        Returns:
            [{"ts_code": "...", "adjusted_score": ..., "delta": ..., ...}, ...]
        """
        results = []
        for c in candidates:
            try:
                ts_code = c.get("ts_code", c.get("code", ""))
                base_score = c.get("total_score", 50)
                result = self.evaluate(ts_code, trade_date,
                                       factor_scores=c.get("factors"),
                                       base_score=base_score)
                result["ts_code"] = ts_code
                results.append(result)
            except Exception:
                results.append({
                    "ts_code": c.get("ts_code", ""),
                    "adjusted_score": c.get("total_score", 50),
                    "delta": 0,
                    "reasons": ["scorecard异常，跳过"],
                    "tags": [],
                    "confidence": "低",
                })
        return results


if __name__ == "__main__":
    print("=" * 50)
    print("  L3 Scorecard 测试")
    print("=" * 50)

    card = Scorecard()
    test_stocks = ["000001.SZ", "600519.SH", "300750.SZ"]

    for ts_code in test_stocks:
        result = card.evaluate(ts_code, "20260703", base_score=70)
        print(f"\n{ts_code}: 分{result['adjusted_score']} (Δ{result['delta']:+}) | 置信度:{result['confidence']}")
        if result["tags"]:
            print(f"  标签: {', '.join(result['tags'])}")
        for r in result["reasons"]:
            print(f"  → {r}")
