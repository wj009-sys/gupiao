"""
测试 Tushare Pro 客户端 — tushare_client.py
"""
import pytest
import pandas as pd
from unittest.mock import patch, MagicMock


class TestTushareClient:
    """tushare_client 核心功能测试"""

    def test_pro_import(self):
        """验证 pro 函数能正确加载（延迟代理模式）"""
        from scripts.utils.tushare_client import pro
        # pro 是 _LazyTushare 实例
        assert hasattr(pro, "daily")
        assert hasattr(pro, "daily_basic")
        assert hasattr(pro, "stock_basic")

    def test_has_token(self):
        """验证 has_token 不崩溃"""
        from scripts.utils.tushare_client import has_token
        result = has_token()
        assert isinstance(result, bool)

    def test_pro_daily_returns_dataframe(self, mock_tushare_pro):
        """验证 daily() 返回 DataFrame"""
        df = mock_tushare_pro.daily(ts_code="000001.SZ")
        assert isinstance(df, pd.DataFrame)
        assert not df.empty
        assert "ts_code" in df.columns
        assert "close" in df.columns
        assert "trade_date" in df.columns

    def test_pro_daily_empty_for_unknown(self, mock_tushare_pro):
        """验证未知股票返回空 DataFrame"""
        df = mock_tushare_pro.daily(ts_code="999999.SZ")
        assert isinstance(df, pd.DataFrame)
        assert df.empty

    def test_pro_daily_basic_has_pe_pb(self, mock_tushare_pro):
        """验证 daily_basic 包含 PE/PB"""
        df = mock_tushare_pro.daily_basic(ts_code="300750.SZ", trade_date="20260703")
        assert not df.empty
        assert "pe" in df.columns
        assert "pb" in df.columns
        assert df.iloc[0]["pe"] == 24.4

    def test_pro_stock_basic_filters(self, mock_tushare_pro):
        """验证 stock_basic 按代码过滤"""
        df_all = mock_tushare_pro.stock_basic()
        assert len(df_all) == 4  # conftest 里定义了 4 只测试股票

        df_filtered = mock_tushare_pro.stock_basic(ts_code="600519.SH")
        assert len(df_filtered) == 1
        assert df_filtered.iloc[0]["name"] == "贵州茅台"

    def test_pro_daily_date_range(self, mock_tushare_pro):
        """验证 daily 带起止日期参数"""
        df = mock_tushare_pro.daily(
            ts_code="000001.SZ",
            start_date="20260701",
            end_date="20260707",
        )
        assert isinstance(df, pd.DataFrame)

    def test_pro_index_daily_returns_data(self, mock_tushare_pro):
        """验证 index_daily 返回数据"""
        df = mock_tushare_pro.index_daily(ts_code="000001.SH")
        assert not df.empty
        if "close" in df.columns:
            assert df["close"].iloc[0] > 0
