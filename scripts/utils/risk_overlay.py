"""
风险叠加层 (Risk Overlay Layer) — 借鉴 AlphaSift 的独立风险惩罚机制

独立于因子评分的惩罚/否决机制，对每只候选股票应用多个风险检查。
每个检查返回 {penalty: float, veto: bool, reasons: []}。

用法：
    overlay = RiskOverlay(profile=scoring_profile)
    result = overlay.apply(ts_code, trade_date)
    # result = {"penalty": -15, "veto": False, "reasons": ["单日涨幅9.5%>阈值9%，惩罚-10分"], "checks_passed": 3, "checks_failed": 1}

D3异常处理表：
| 触发条件 | 一线修复 | 仍失败兜底 |
|---------|---------|-----------|
| 行情数据缺失 | 跳过该风险检查 | 返回空惩罚（penalty=0） |
| PE数据异常（None/NaN） | 跳过PE相关检查 | 默认不惩罚（假设PE正常） |
| 涨跌停价计算失败 | 用常规±10%代替 | 跳过涨跌停检查 |
"""

import numpy as np
import pandas as pd
import sys, os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from scripts.utils.tushare_client import pro
from scripts.utils.db_manager import get_daily_price_db_first


class RiskCheck:
    """风险检查基类：每个子类实现 check() 方法"""
    def check(self, ts_code: str, trade_date: str, **kwargs) -> dict:
        """返回 {penalty: float, veto: bool, reason: str}"""
        return {"penalty": 0.0, "veto": False, "reason": ""}


class DailyChangeCheck(RiskCheck):
    """单日涨跌过大惩罚 — 超过阈值则否决或惩罚"""

    def __init__(self, threshold: float = 9.0, veto_threshold: float = 11.0):
        self.threshold = threshold
        self.veto_threshold = veto_threshold

    def check(self, ts_code: str, trade_date: str, **kwargs) -> dict:
        try:
            df = pro.daily(ts_code=ts_code, start_date=trade_date, end_date=trade_date)
            if df is None or df.empty:
                return {"penalty": 0, "veto": False, "reason": "无当日行情"}
            change_pct = abs(float(df.iloc[0].get("pct_chg", 0)))
            if change_pct >= self.veto_threshold:
                return {"penalty": 100, "veto": True,
                        "reason": f"单日涨跌幅{change_pct:.1f}%>否决阈值{self.veto_threshold}%，否决"}
            if change_pct >= self.threshold:
                penalty = min(30, (change_pct - self.threshold) * 10)
                return {"penalty": penalty, "veto": False,
                        "reason": f"单日涨跌幅{change_pct:.1f}%>阈值{self.threshold}%，惩罚-{penalty:.0f}分"}
            return {"penalty": 0, "veto": False, "reason": ""}
        except Exception as e:
            return {"penalty": 0, "veto": False, "reason": f"涨跌检查异常: {e}"}


class VolumeRatioCheck(RiskCheck):
    """量比异常检查 — 过高量比=投机过热"""

    def __init__(self, max_ratio: float = 5.0, penalty_per_unit: float = 5.0):
        self.max_ratio = max_ratio
        self.penalty_per_unit = penalty_per_unit

    def check(self, ts_code: str, trade_date: str, **kwargs) -> dict:
        try:
            df = pro.daily_basic(ts_code=ts_code, trade_date=trade_date)
            if df is None or df.empty:
                return {"penalty": 0, "veto": False, "reason": "无数据"}
            vol_ratio = float(df.iloc[0].get("volume_ratio", 1))
            if vol_ratio > self.max_ratio:
                penalty = min(25, (vol_ratio - self.max_ratio) * self.penalty_per_unit)
                return {"penalty": penalty, "veto": False,
                        "reason": f"量比{vol_ratio:.1f}>阈值{self.max_ratio}，惩罚-{penalty:.0f}分"}
            return {"penalty": 0, "veto": False, "reason": ""}
        except Exception as e:
            print(f'  [WARN] risk_overlay 量比检查异常 ({ts_code}): {e}')
            return {"penalty": 0, "veto": False, "reason": "量比检查异常"}


class NegativePECheck(RiskCheck):
    """负PE惩罚"""

    def check(self, ts_code: str, trade_date: str, **kwargs) -> dict:
        try:
            df = pro.daily_basic(ts_code=ts_code, trade_date=trade_date)
            if df is None or df.empty:
                return {"penalty": 0, "veto": False, "reason": "无PE数据"}
            pe = df.iloc[0].get("pe")
            if pe is not None and pe < 0:
                return {"penalty": 20, "veto": False,
                        "reason": f"PE={pe}<0(亏损)，惩罚-20分"}
            return {"penalty": 0, "veto": False, "reason": ""}
        except Exception as e:
            print(f'  [WARN] risk_overlay PE检查异常 ({ts_code}): {e}')
            return {"penalty": 0, "veto": False, "reason": "PE检查异常"}


class MacdWeakCheck(RiskCheck):
    """MACD弱势惩罚 — DEA线向下且为负值时惩罚"""

    def check(self, ts_code: str, trade_date: str, **kwargs) -> dict:
        try:
            from scripts.utils.technical_analysis import add_all_indicators
            df = get_daily_price_db_first(ts_code, days_back=60, caller="risk_overlay")
            if df is None or df.empty or len(df) < 35:
                return {"penalty": 0, "veto": False, "reason": "数据不足"}
            # 转为升序（technical_analysis需要升序数据）
            df_asc = df.sort_values("trade_date", ascending=True).reset_index(drop=True)
            df_with_ind = add_all_indicators(df_asc)
            if df_with_ind is None or df_with_ind.empty:
                return {"penalty": 0, "veto": False, "reason": "指标计算失败"}
            latest = df_with_ind.iloc[-1]
            # ⚠️ 使用小写字段名：add_all_indicators()输出全小写（macd_diff/macd_signal），非大写MACD_diff/MACD_dea
            macd_diff = latest.get("macd_diff")
            macd_signal = latest.get("macd_signal")
            if macd_diff is not None and macd_signal is not None:
                if macd_diff < 0 and macd_signal < 0 and macd_signal < macd_diff:
                    return {"penalty": 15, "veto": False,
                            "reason": f"MACD弱势(diff={macd_diff:.2f}, signal={macd_signal:.2f})，惩罚-15分"}
                if macd_diff < 0:
                    return {"penalty": 8, "veto": False,
                            "reason": f"MACD偏弱(diff={macd_diff:.2f})，惩罚-8分"}
            return {"penalty": 0, "veto": False, "reason": ""}
        except Exception as e:
            print(f'  [WARN] risk_overlay MACD检查异常 ({ts_code}): {e}')
            return {"penalty": 0, "veto": False, "reason": "MACD检查异常"}


class HighPBCheck(RiskCheck):
    """高PB惩罚"""

    def __init__(self, pb_threshold: float = 10.0):
        self.pb_threshold = pb_threshold

    def check(self, ts_code: str, trade_date: str, **kwargs) -> dict:
        try:
            df = pro.daily_basic(ts_code=ts_code, trade_date=trade_date)
            if df is None or df.empty:
                return {"penalty": 0, "veto": False, "reason": "无PB数据"}
            pb = df.iloc[0].get("pb")
            if pb is not None and pb > self.pb_threshold:
                return {"penalty": 10, "veto": False,
                        "reason": f"PB={pb:.1f}>阈值{self.pb_threshold}，惩罚-10分"}
            return {"penalty": 0, "veto": False, "reason": ""}
        except Exception as e:
            print(f'  [WARN] risk_overlay PB检查异常 ({ts_code}): {e}')
            return {"penalty": 0, "veto": False, "reason": "PB检查异常"}


class ConsecutiveDropCheck(RiskCheck):
    """连续下跌惩罚 — 连跌3天及以上"""
    def check(self, ts_code: str, trade_date: str, **kwargs) -> dict:
        try:
            df = get_daily_price_db_first(ts_code, days_back=10)
            if df is None or df.empty or len(df) < 5:
                return {"penalty": 0, "veto": False, "reason": "数据不足"}
            pct_chgs = df["pct_chg"].values[:5]
            consecutive_neg = 0
            for v in pct_chgs:
                if v < 0:
                    consecutive_neg += 1
                else:
                    break
            if consecutive_neg >= 4:
                return {"penalty": 20, "veto": False,
                        "reason": f"连跌{consecutive_neg}天，惩罚-20分"}
            if consecutive_neg >= 3:
                return {"penalty": 10, "veto": False,
                        "reason": f"连跌{consecutive_neg}天，惩罚-10分"}
            return {"penalty": 0, "veto": False, "reason": ""}
        except Exception as e:
            print(f'  [WARN] risk_overlay 连跌检查异常 ({ts_code}): {e}')
            return {"penalty": 0, "veto": False, "reason": "连跌检查异常"}


class RiskOverlay:
    """
    风险叠加层：集成多个风险检查，对候选股票进行独立风险评分

    用法：
        overlay = RiskOverlay()
        result = overlay.apply_all(ts_code, trade_date)
        # 返回累积惩罚和否决状态
    """

    def __init__(self, profile: dict = None):
        self.checks = [
            DailyChangeCheck(threshold=profile.get("stability_hot_change_pct", 9.0) if profile else 9.0,
                             veto_threshold=11.0),
            VolumeRatioCheck(max_ratio=profile.get("stability_extreme_volume_ratio", 5.0) if profile else 5.0),
            NegativePECheck(),
            MacdWeakCheck(),
            HighPBCheck(pb_threshold=10.0),
            ConsecutiveDropCheck(),
        ]

    def apply_all(self, ts_code: str, trade_date: str) -> dict:
        """
        应用所有风险检查，返回累积结果

        Returns:
            {"penalty": float, "veto": bool, "reasons": [str],
             "checks_total": int, "checks_failed": int}
        """
        total_penalty = 0.0
        is_vetoed = False
        all_reasons = []
        checks_failed = 0
        checks_passed = 0

        for check in self.checks:
            try:
                result = check.check(ts_code, trade_date)
                if result["veto"]:
                    is_vetoed = True
                    all_reasons.append(f"[否决] {result['reason']}")
                    checks_failed += 1
                elif result["penalty"] > 0:
                    total_penalty += result["penalty"]
                    all_reasons.append(result["reason"])
                    checks_failed += 1
                else:
                    checks_passed += 1
            except Exception as e:
                print(f'  [WARN] risk_overlay 风险检查异常 ({ts_code}, {type(check).__name__}): {e}')
                checks_passed += 1  # 异常视为检查通过（容错）

        return {
            "penalty": round(total_penalty, 1),
            "veto": is_vetoed,
            "reasons": all_reasons,
            "checks_total": len(self.checks),
            "checks_failed": checks_failed,
            "checks_passed": checks_passed,
        }

    def apply_to_stock(self, ts_code: str, trade_date: str, current_score: float) -> dict:
        """
        对单只股票应用风险叠加，返回调整后的评分

        Args:
            ts_code: 股票代码
            trade_date: 交易日
            current_score: 因子评分base score (0-100)

        Returns:
            {"adjusted_score": float, "penalty_applied": float,
             "veto": bool, "reasons": [str]}
        """
        risk = self.apply_all(ts_code, trade_date)
        if risk["veto"]:
            return {
                "adjusted_score": 0,
                "penalty_applied": current_score,
                "veto": True,
                "reasons": risk["reasons"],
            }
        penalty_applied = min(current_score, risk["penalty"])
        adjusted_score = max(0, current_score - penalty_applied)
        return {
            "adjusted_score": round(adjusted_score, 1),
            "penalty_applied": round(penalty_applied, 1),
            "veto": False,
            "reasons": risk["reasons"],
        }


# ============================================================
#  独立运行测试
# ============================================================
if __name__ == "__main__":
    print("=" * 50)
    print("  风险叠加层测试")
    print("=" * 50)

    overlay = RiskOverlay()
    test_stocks = ["000001.SZ", "600519.SH", "300750.SZ"]
    trade_date = "20260703"

    for ts_code in test_stocks:
        result = overlay.apply_all(ts_code, trade_date)
        status = "❌ 否决!" if result["veto"] else f"惩罚-{result['penalty']:.0f}分"
        print(f"\n{ts_code}: {status}")
        print(f"  检查: {result['checks_passed']}/{result['checks_total']} 通过")
        for r in result["reasons"]:
            print(f"  → {r}")
