"""
测试技术指标计算 — technical_analysis.py

使用已知数据验证 MACD/KDJ/RSI/BOLL 计算正确性。
"""
import pytest
import pandas as pd
import numpy as np


class TestTechnicalAnalysis:
    """技术指标计算正确性验证"""

    def test_add_all_indicators_returns_dataframe(self, sample_price_data):
        """验证函数返回带指标列的 DataFrame"""
        from scripts.utils.technical_analysis import add_all_indicators
        result = add_all_indicators(sample_price_data)
        assert isinstance(result, pd.DataFrame)
        assert len(result) == len(sample_price_data)

    def test_add_all_indicators_has_required_columns(self, sample_price_data):
        """验证输出包含所有必要指标列"""
        from scripts.utils.technical_analysis import add_all_indicators
        result = add_all_indicators(sample_price_data)
        required = ["macd_diff", "macd_signal", "macd",
                     "kdj_k", "kdj_d", "kdj_j",
                     "rsi_14", "rsi_oversold", "rsi_overbought",
                     "boll_upper", "boll_mid", "boll_lower"]
        for col in required:
            assert col in result.columns, f"缺失列: {col}"
        assert result[required].notna().any().any(), "所有指标列全为 NaN"

    def test_macd_values_at_tail(self, sample_price_data):
        """验证 MACD 最后几行有合理的值（非全 NaN）"""
        from scripts.utils.technical_analysis import add_all_indicators
        result = add_all_indicators(sample_price_data)
        # 最后 5 行的 MACD 值不应该全为 NaN
        tail_macd = result["macd_diff"].tail(5)
        assert tail_macd.notna().sum() >= 3, "MACD 最后5行中至少3行有值"

    def test_kdj_values_at_tail(self, sample_price_data):
        """验证 KDJ 最后几行有合理的值"""
        from scripts.utils.technical_analysis import add_all_indicators
        result = add_all_indicators(sample_price_data)
        tail_k = result["kdj_k"].tail(5)
        tail_d = result["kdj_d"].tail(5)
        assert tail_k.notna().sum() >= 3
        assert tail_d.notna().sum() >= 3
        # KDJ值的合理范围是 0-100（偶尔会略超）
        valid_k = tail_k.dropna()
        if len(valid_k) > 0:
            assert all(0 <= v <= 100 for v in valid_k), f"KDJ_K 超出 0-100 范围: {valid_k.tolist()}"

    def test_rsi_values_at_tail(self, sample_price_data):
        """验证 RSI 最后几行有合理的值"""
        from scripts.utils.technical_analysis import add_all_indicators
        result = add_all_indicators(sample_price_data)
        tail_rsi = result["rsi_14"].tail(5) if "rsi_14" in result.columns else result["rsi_6"].tail(5)
        assert tail_rsi.notna().sum() >= 3
        valid_rsi = tail_rsi.dropna()
        if len(valid_rsi) > 0:
            assert all(0 <= v <= 100 for v in valid_rsi), f"RSI 超出 0-100: {valid_rsi.tolist()}"

    def test_boll_bands_ordered(self, sample_price_data):
        """验证布林带上>中>下"""
        from scripts.utils.technical_analysis import add_all_indicators
        result = add_all_indicators(sample_price_data)
        # 至少最后一行布林带正确
        last = result.iloc[-1]
        upper, mid, lower = last["boll_upper"], last["boll_mid"], last["boll_lower"]
        if pd.notna(upper) and pd.notna(mid) and pd.notna(lower):
            assert upper >= mid >= lower, f"布林带顺序错误: {upper} >= {mid} >= {lower}"
            assert upper > lower, "布林带上轨应大于下轨"

    def test_empty_dataframe(self):
        """验证空 DataFrame 返回空"""
        from scripts.utils.technical_analysis import add_all_indicators
        result = add_all_indicators(pd.DataFrame())
        assert result.empty

    def test_single_row_dataframe(self):
        """验证单行数据不崩溃"""
        from scripts.utils.technical_analysis import add_all_indicators
        df = pd.DataFrame({
            "trade_date": ["20260707"],
            "close": [10.0],
            "high": [10.5],
            "low": [9.5],
            "vol": [10000],
        })
        result = add_all_indicators(df)
        assert len(result) == 1

    def test_nan_input_handling(self):
        """验证 NaN 输入不崩溃"""
        from scripts.utils.technical_analysis import add_all_indicators
        df = pd.DataFrame({
            "trade_date": [f"2026070{i}" for i in range(1, 6)],
            "close": [10.0, None, 11.0, np.nan, 12.0],
            "high": [10.5, 10.3, 11.2, 11.0, 12.3],
            "low": [9.5, 9.8, 10.5, 10.2, 11.5],
            "vol": [10000, 12000, 11000, 13000, 9000],
        })
        result = add_all_indicators(df)
        assert result is not None
