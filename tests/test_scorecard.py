"""
测试 L3 Scorecard — scorecard.py

验证 5 条规则 + RiskOverlayRule 集成的正确性。
"""
import pytest
from unittest.mock import patch, MagicMock


class TestScorecardRules:
    """各规则独立测试"""

    def test_scorecard_init_with_risk_overlay(self):
        """验证 Scorecard 默认包含 RiskOverlayRule"""
        from scripts.utils.scorecard import Scorecard
        card = Scorecard(enable_risk_overlay=True)
        rule_names = [r.name for r in card.rules]
        assert "突破确认" in rule_names
        assert "量价配合" in rule_names
        assert "均线排列" in rule_names
        assert "板块动量" in rule_names
        assert "基本面确认" in rule_names
        assert "风险叠加" in rule_names

    def test_scorecard_init_without_risk_overlay(self):
        """验证可禁用 RiskOverlayRule"""
        from scripts.utils.scorecard import Scorecard
        card = Scorecard(enable_risk_overlay=False)
        rule_names = [r.name for r in card.rules]
        assert "风险叠加" not in rule_names
        assert len(card.rules) == 5

    def test_evaluate_returns_correct_keys(self):
        """验证 evaluate 返回结构"""
        from scripts.utils.scorecard import Scorecard
        card = Scorecard(enable_risk_overlay=False)
        result = card.evaluate("000001.SZ", "20260703", base_score=70)
        assert "adjusted_score" in result
        assert "original_score" in result
        assert "delta" in result
        assert "reasons" in result
        assert "tags" in result
        assert "confidence" in result

    def test_evaluate_batch_returns_list(self):
        """验证批量评估返回列表"""
        from scripts.utils.scorecard import Scorecard
        card = Scorecard(enable_risk_overlay=False)
        candidates = [
            {"ts_code": "000001.SZ", "total_score": 70},
            {"ts_code": "600519.SH", "total_score": 80},
        ]
        results = card.evaluate_batch(candidates, "20260703")
        assert len(results) == 2
        for r in results:
            assert "ts_code" in r
            assert "adjusted_score" in r

    def test_breakout_confirm_rule(self):
        """验证 BreakoutConfirmRule 不崩溃"""
        from scripts.utils.scorecard import BreakoutConfirmRule
        rule = BreakoutConfirmRule()
        result = rule.apply("000001.SZ", "20260703")
        assert "delta" in result
        assert "reasons" in result

    def test_volume_confirm_rule(self):
        """验证 VolumeConfirmRule 不崩溃"""
        from scripts.utils.scorecard import VolumeConfirmRule
        rule = VolumeConfirmRule()
        result = rule.apply("000001.SZ", "20260703")
        assert "delta" in result
        assert "reasons" in result

    def test_ma_arrangement_rule(self):
        """验证 MaArrangementRule 不崩溃"""
        from scripts.utils.scorecard import MaArrangementRule
        rule = MaArrangementRule()
        result = rule.apply("000001.SZ", "20260703")
        assert isinstance(result["delta"], (int, float))

    def test_fundamental_confirm_rule(self):
        """验证 FundamentalConfirmRule 不崩溃"""
        from scripts.utils.scorecard import FundamentalConfirmRule
        with patch("scripts.utils.scorecard.pro") as mock_pro:
            class MockRow:
                def get(self, key, default=None):
                    vals = {"pe": 4.7, "pb": 0.4}
                    return vals.get(key, default)
            mock_df = MagicMock()
            mock_df.empty = False
            mock_df.iloc = MagicMock()
            mock_df.iloc.__getitem__.return_value = MockRow()
            mock_pro.daily_basic.return_value = mock_df

            rule = FundamentalConfirmRule()
            result = rule.apply("000001.SZ", "20260703")
            assert isinstance(result["delta"], (int, float))

    def test_risk_overlay_rule_penalty(self):
        """验证 RiskOverlayRule 惩罚生效"""
        from scripts.utils.scorecard import RiskOverlayRule
        rule = RiskOverlayRule()
        result = rule.apply("000001.SZ", "20260703")
        # 至少不崩溃且有正确返回结构
        assert "delta" in result
        assert isinstance(result["delta"], (int, float))
        assert result["delta"] <= 0  # 风险叠加只扣分不加分
