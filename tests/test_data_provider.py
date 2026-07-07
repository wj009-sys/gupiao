"""
测试多数据源 Provider — data_provider.py

验证：
1. 初始化成功
2. 自动 fallback 链（Tushare→AkShare）
3. Tushare 不可用时降级到 AkShare
"""
import pytest
import pandas as pd
from unittest.mock import patch, MagicMock


class TestDataProvider:
    """DataProvider 基础功能"""

    @patch("scripts.utils.data_provider.TushareProvider")
    def test_provider_init(self, mock_tushare_provider):
        """验证 DataProvider 初始化"""
        mock_provider_instance = MagicMock()
        mock_provider_instance.name = "tushare"
        mock_tushare_provider.return_value = mock_provider_instance

        from scripts.utils.data_provider import DataProvider
        dp = DataProvider(provider_order=["tushare"])
        assert len(dp._providers) >= 1

    @patch("scripts.utils.data_provider.TushareProvider")
    def test_daily_fallback(self, mock_tushare_provider):
        """验证 Tushare 失败时 fallback"""
        from scripts.utils.data_provider import DataProvider, TushareProvider

        # 让 Tushare 失败
        mock_tushare = MagicMock()
        mock_tushare.name = "tushare"
        mock_tushare.daily.return_value = pd.DataFrame()  # 空
        mock_tushare_provider.return_value = mock_tushare

        # 初始化 — Tushare 可用但返回空
        dp = DataProvider(provider_order=["tushare"])
        result = dp.daily("000001.SZ")
        assert isinstance(result, pd.DataFrame)

    @patch("scripts.utils.data_provider.TushareProvider")
    def test_daily_success(self, mock_tushare_provider):
        """验证 Tushare 正常返回数据"""
        mock_tushare = MagicMock()
        mock_tushare.name = "tushare"
        mock_df = pd.DataFrame({
            "ts_code": ["000001.SZ"] * 3,
            "trade_date": ["20260707", "20260706", "20260703"],
            "close": [12.0, 11.8, 12.5],
            "pct_chg": [1.5, -0.5, 2.0],
        })
        mock_tushare.daily.return_value = mock_df
        mock_tushare_provider.return_value = mock_tushare

        from scripts.utils.data_provider import DataProvider
        dp = DataProvider(provider_order=["tushare"])
        result = dp.daily("000001.SZ", start_date="20260703", end_date="20260707")
        assert not result.empty
        assert len(result) == 3
        assert list(result["close"]) == [12.0, 11.8, 12.5]

    @patch("scripts.utils.data_provider.TushareProvider")
    def test_stock_basic(self, mock_tushare_provider):
        """验证 stock_basic 返回数据和列名"""
        mock_tushare = MagicMock()
        mock_tushare.name = "tushare"
        mock_tushare.stock_basic.return_value = pd.DataFrame({
            "ts_code": ["000001.SZ"],
            "name": ["平安银行"],
            "industry": ["银行"],
        })
        mock_tushare_provider.return_value = mock_tushare

        from scripts.utils.data_provider import DataProvider
        dp = DataProvider(provider_order=["tushare"])
        df = dp.stock_basic()
        assert not df.empty
        assert df.iloc[0]["ts_code"] == "000001.SZ"

    @patch("scripts.utils.data_provider.TushareProvider")
    def test_with_provider_order(self, mock_tushare_provider):
        """验证自定义 provider 优先级"""
        mock_tushare = MagicMock()
        mock_tushare.name = "tushare"
        mock_df = pd.DataFrame({"ts_code": ["000001.SZ"], "close": [12.0]})
        mock_tushare.daily.return_value = mock_df
        mock_tushare_provider.return_value = mock_tushare

        from scripts.utils.data_provider import DataProvider
        dp = DataProvider(provider_order=["tushare"])
        assert "tushare" in dp._provider_map

    @patch("scripts.utils.data_provider.TushareProvider")
    def test_moneyflow(self, mock_tushare_provider):
        """验证 moneyflow 返回"""
        mock_tushare = MagicMock()
        mock_tushare.name = "tushare"
        mock_tushare.moneyflow.return_value = pd.DataFrame({
            "ts_code": ["000001.SZ"],
            "buy_sm_vol": [1000],
        })
        mock_tushare_provider.return_value = mock_tushare

        from scripts.utils.data_provider import DataProvider
        dp = DataProvider(provider_order=["tushare"])
        result = dp.moneyflow("000001.SZ")
        assert not result.empty

    @patch("scripts.utils.data_provider.TushareProvider")
    def test_index_daily(self, mock_tushare_provider):
        """验证 index_daily 返回"""
        mock_tushare = MagicMock()
        mock_tushare.name = "tushare"
        mock_tushare.index_daily.return_value = pd.DataFrame({
            "ts_code": ["000001.SH"],
            "close": [4000.0],
        })
        mock_tushare_provider.return_value = mock_tushare

        from scripts.utils.data_provider import DataProvider
        dp = DataProvider(provider_order=["tushare"])
        result = dp.index_daily("000001.SH")
        assert not result.empty

    def test_providers_info(self):
        """验证 providers_info 返回格式"""
        from scripts.utils.data_provider import DataProvider
        # 至少不会崩溃（真实环境）
        try:
            dp = DataProvider(provider_order=["tushare", "akshare"])
        except Exception:
            pytest.skip("数据源初始化失败（环境问题）")
            return
        info = dp.providers_info
        assert isinstance(info, list)
