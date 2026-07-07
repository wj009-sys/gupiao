"""
测试风险叠加层 — risk_overlay.py

验证 6 项风险检查的独立和组合效果。
"""
import pytest
from unittest.mock import patch, MagicMock, PropertyMock


class TestRiskChecks:
    """各项风险检查独立测试"""

    @patch("scripts.utils.risk_overlay.pro")
    def test_daily_change_check_normal(self, mock_pro):
        """验证正常涨幅不触发惩罚"""
        from scripts.utils.risk_overlay import DailyChangeCheck

        class MockRow:
            def get(self, key, default=0):
                return 1.5 if key == "pct_chg" else default

        mock_df = MagicMock()
        mock_df.empty = False
        mock_df.__len__.return_value = 1
        mock_df.iloc = MagicMock()
        mock_df.iloc.__getitem__.return_value = MockRow()
        mock_pro.daily.return_value = mock_df

        check = DailyChangeCheck(threshold=9.0, veto_threshold=11.0)
        result = check.check("000001.SZ", "20260703")
        assert result["penalty"] == 0
        assert result["veto"] is False

    @patch("scripts.utils.risk_overlay.pro")
    def test_daily_change_high_penalty(self, mock_pro):
        """验证大涨触发惩罚"""
        from scripts.utils.risk_overlay import DailyChangeCheck

        class MockRow:
            def get(self, key, default=0):
                return 10.0 if key == "pct_chg" else default

        mock_df = MagicMock()
        mock_df.empty = False
        mock_df.__len__.return_value = 1
        mock_df.iloc = MagicMock()
        mock_df.iloc.__getitem__.return_value = MockRow()
        mock_pro.daily.return_value = mock_df

        check = DailyChangeCheck(threshold=9.0, veto_threshold=11.0)
        result = check.check("000001.SZ", "20260703")
        assert result["penalty"] > 0

    @patch("scripts.utils.risk_overlay.pro")
    def test_daily_change_veto(self, mock_pro):
        """验证超大涨幅触发否决"""
        from scripts.utils.risk_overlay import DailyChangeCheck

        class MockRow:
            def get(self, key, default=0):
                return 12.0 if key == "pct_chg" else default

        mock_df = MagicMock()
        mock_df.empty = False
        mock_df.__len__.return_value = 1
        mock_df.iloc = MagicMock()
        mock_df.iloc.__getitem__.return_value = MockRow()
        mock_pro.daily.return_value = mock_df

        check = DailyChangeCheck(threshold=9.0, veto_threshold=11.0)
        result = check.check("000001.SZ", "20260703")
        assert result["veto"] is True

    @patch("scripts.utils.risk_overlay.pro")
    def test_negative_pe_penalty(self, mock_pro):
        """验证负PE触发惩罚"""
        from scripts.utils.risk_overlay import NegativePECheck

        class MockRow:
            def get(self, key, default=None):
                if key == "pe":
                    return -5.0
                return default

        mock_df = MagicMock()
        mock_df.empty = False
        mock_df.iloc = MagicMock()
        mock_df.iloc.__getitem__.return_value = MockRow()
        mock_pro.daily_basic.return_value = mock_df

        check = NegativePECheck()
        result = check.check("000001.SZ", "20260703")
        assert result["penalty"] > 0

    @patch("scripts.utils.risk_overlay.pro")
    def test_high_pb_penalty(self, mock_pro):
        """验证高PB触发惩罚"""
        from scripts.utils.risk_overlay import HighPBCheck

        class MockRow:
            def get(self, key, default=None):
                if key == "pb":
                    return 15.0
                return default

        mock_df = MagicMock()
        mock_df.empty = False
        mock_df.iloc = MagicMock()
        mock_df.iloc.__getitem__.return_value = MockRow()
        mock_pro.daily_basic.return_value = mock_df

        check = HighPBCheck(pb_threshold=10.0)
        result = check.check("000001.SZ", "20260703")
        assert result["penalty"] > 0

    @patch("scripts.utils.risk_overlay.pro")
    @patch("scripts.utils.risk_overlay.get_daily_price_db_first")
    def test_consecutive_drop_detected(self, mock_db, mock_pro):
        """验证连跌检测（4天连跌）"""
        from scripts.utils.risk_overlay import ConsecutiveDropCheck
        import pandas as pd
        import numpy as np

        # 构造降序数据，前5天全跌
        df = pd.DataFrame({
            "pct_chg": [-1.0, -2.0, -1.5, -0.5, -3.0, 1.0, 0.5],
        })
        mock_db.return_value = df

        check = ConsecutiveDropCheck()
        result = check.check("000001.SZ", "20260703")
        # 前4天都跌 → 惩罚
        assert result["penalty"] > 0

    @patch("scripts.utils.risk_overlay.pro")
    @patch("scripts.utils.risk_overlay.get_daily_price_db_first")
    def test_consecutive_drop_no_penalty(self, mock_db, mock_pro):
        """验证非连跌序列不惩罚"""
        from scripts.utils.risk_overlay import ConsecutiveDropCheck
        import pandas as pd

        # 构造降序数据，有涨有跌
        df = pd.DataFrame({
            "pct_chg": [1.0, -2.0, 1.5, -0.5, -1.0, 1.0, 0.5],
        })
        mock_db.return_value = df

        check = ConsecutiveDropCheck()
        result = check.check("000001.SZ", "20260703")
        assert result["penalty"] == 0

    @patch("scripts.utils.risk_overlay.pro")
    def test_empty_data_handling(self, mock_pro):
        """验证空数据不崩溃"""
        from scripts.utils.risk_overlay import DailyChangeCheck
        mock_pro.daily.return_value = MagicMock(empty=True)

        check = DailyChangeCheck()
        result = check.check("000001.SZ", "20260703")
        assert result["penalty"] == 0
        assert result["veto"] is False


class TestRiskOverlay:
    """RiskOverlay 组合测试"""

    def test_risk_overlay_init(self):
        """验证 RiskOverlay 初始化"""
        from scripts.utils.risk_overlay import RiskOverlay
        overlay = RiskOverlay()
        assert len(overlay.checks) == 6
        check_names = [type(c).__name__ for c in overlay.checks]
        assert "DailyChangeCheck" in check_names
        assert "NegativePECheck" in check_names
        assert "MacdWeakCheck" in check_names
        assert "ConsecutiveDropCheck" in check_names

    @patch("scripts.utils.risk_overlay.get_daily_price_db_first")
    @patch("scripts.utils.risk_overlay.pro")
    def test_apply_all_returns_correct_structure(self, mock_pro, mock_db):
        """验证 apply_all 返回正确结构"""
        from scripts.utils.risk_overlay import RiskOverlay
        import pandas as pd

        # Mock 所有 pro 调用
        mock_df = MagicMock()
        mock_df.empty = False
        mock_df.__len__.return_value = 1

        class MockZeroRow:
            def get(self, key, default=0):
                return 0.0 if key in ("pct_chg", "pe", "pb", "volume_ratio") else default

        mock_df.iloc = MagicMock()
        mock_df.iloc.__getitem__.return_value = MockZeroRow()
        mock_pro.daily.return_value = mock_df
        mock_pro.daily_basic.return_value = mock_df

        # Mock DB
        df_db = pd.DataFrame({"pct_chg": [1.0, -0.5, 0.5, -1.0, 2.0, 0.0, 0.0, 0.0, 0.0, 0.0]})
        mock_db.return_value = df_db

        overlay = RiskOverlay()
        result = overlay.apply_all("000001.SZ", "20260703")
        assert "penalty" in result
        assert "veto" in result
        assert "reasons" in result
        assert "checks_total" in result
        assert "checks_passed" in result
        assert "checks_failed" in result
        assert isinstance(result["penalty"], (int, float))
        print(f"  Result: {result}")  # 调试

    @patch("scripts.utils.risk_overlay.get_daily_price_db_first")
    @patch("scripts.utils.risk_overlay.pro")
    def test_apply_to_stock_contains_all_keys(self, mock_pro, mock_db):
        """验证 apply_to_stock 返回结构正确"""
        from scripts.utils.risk_overlay import RiskOverlay
        import pandas as pd

        mock_df = MagicMock()
        mock_df.empty = False
        mock_df.__len__.return_value = 1

        class MockZeroRow:
            def get(self, key, default=0):
                return 0.0 if key in ("pct_chg", "pe", "pb", "volume_ratio") else default

        mock_df.iloc = MagicMock()
        mock_df.iloc.__getitem__.return_value = MockZeroRow()
        mock_pro.daily.return_value = mock_df
        mock_pro.daily_basic.return_value = mock_df

        df_db = pd.DataFrame({"pct_chg": [1.0, -0.5, 0.5, -1.0, 2.0, 0.0, 0.0, 0.0, 0.0, 0.0]})
        mock_db.return_value = df_db

        overlay = RiskOverlay()
        result = overlay.apply_to_stock("000001.SZ", "20260703", current_score=80)
        assert "adjusted_score" in result
        assert "penalty_applied" in result
        assert "veto" in result
        assert "reasons" in result
        assert 0 <= result["adjusted_score"] <= 80
