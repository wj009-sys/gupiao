"""
pytest 共享配置 — Mock Tushare、Mock DB、样本数据

用法：
    pip install pytest pytest-mock
    pytest tests/ -v
"""
import os
import sys
import json
import pytest
import numpy as np
import pandas as pd
from unittest.mock import MagicMock, patch

# 确保项目根在 sys.path
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, PROJECT_ROOT)


# ============================================================
#  Fixture: 模拟 Tushare Pro API
# ============================================================

@pytest.fixture
def mock_tushare_pro():
    """Mock Tushare Pro 客户端的所有核心 API

    使用前需 patch('scripts.utils.tushare_client.pro', 返回此 mock)
    """
    mock = MagicMock()

    # ── daily ──
    def _mock_daily(ts_code="", start_date="", end_date="", **kwargs):
        if ts_code in ("000001.SZ", "600519.SH"):
            n = 60
            base_close = 15.0 if ts_code == "000001.SZ" else 1800.0
            dates = _gen_trade_dates(n)
            closes = base_close + np.cumsum(np.random.default_rng(42).normal(0, 0.3, n))
            df = pd.DataFrame({
                "ts_code": [ts_code] * n,
                "trade_date": dates,
                "open": closes * (1 + np.random.default_rng(42).normal(0, 0.005, n)),
                "high": closes * (1 + np.abs(np.random.default_rng(42).normal(0, 0.01, n))),
                "low": closes * (1 - np.abs(np.random.default_rng(42).normal(0, 0.01, n))),
                "close": closes,
                "vol": np.random.default_rng(42).poisson(10000, n),
                "amount": closes * np.random.default_rng(42).poisson(10000, n),
                "pct_chg": np.random.default_rng(42).normal(0, 1.5, n),
                "change": np.random.default_rng(42).normal(0, 0.3, n),
            })
            return df.sort_values("trade_date", ascending=False).reset_index(drop=True)
        return pd.DataFrame()

    mock.daily = MagicMock(side_effect=_mock_daily)

    # ── daily_basic ──
    def _mock_daily_basic(ts_code="", trade_date="", **kwargs):
        pe_map = {
            "000001.SZ": 4.7,
            "600519.SH": 25.0,
            "300750.SZ": 24.4,
        }
        pb_map = {
            "000001.SZ": 0.4,
            "600519.SH": 8.0,
            "300750.SZ": 4.9,
        }
        df = pd.DataFrame([{
            "ts_code": ts_code,
            "trade_date": trade_date,
            "pe": pe_map.get(ts_code, 20.0),
            "pb": pb_map.get(ts_code, 2.0),
            "volume_ratio": 1.2,
            "turnover_rate": 2.5,
            "total_mv": 10000.0,
            "circ_mv": 8000.0,
        }])
        return df

    mock.daily_basic = MagicMock(side_effect=_mock_daily_basic)

    # ── stock_basic ──
    def _mock_stock_basic(ts_code="", fields="", **kwargs):
        stocks = [
            {"ts_code": "000001.SZ", "name": "平安银行", "industry": "银行", "area": "深圳"},
            {"ts_code": "600519.SH", "name": "贵州茅台", "industry": "白酒", "area": "贵州"},
            {"ts_code": "300750.SZ", "name": "宁德时代", "industry": "电气设备", "area": "福建"},
            {"ts_code": "600388.SH", "name": "龙净环保", "industry": "环境保护", "area": "福建"},
        ]
        if ts_code:
            stocks = [s for s in stocks if s["ts_code"] == ts_code]
        df = pd.DataFrame(stocks)
        if fields:
            cols = [c for c in fields.split(",") if c in df.columns]
            if "ts_code" in fields:
                cols = ["ts_code"] + [c for c in cols if c != "ts_code"]
            df = df[cols] if cols else df
        return df

    mock.stock_basic = MagicMock(side_effect=_mock_stock_basic)

    # ── index_daily ──
    def _mock_index_daily(ts_code="", start_date="", end_date="", **kwargs):
        n = 30
        df = pd.DataFrame({
            "ts_code": [ts_code] * n,
            "trade_date": _gen_trade_dates(n),
            "close": 3000 + np.cumsum(np.random.default_rng(42).normal(0, 10, n)),
            "pct_chg": np.random.default_rng(42).normal(0, 0.5, n),
            "vol": np.random.default_rng(42).poisson(5000000, n),
        })
        return df.sort_values("trade_date", ascending=False).reset_index(drop=True)

    mock.index_daily = MagicMock(side_effect=_mock_index_daily)

    # ── moneyflow ──
    def _mock_moneyflow(ts_code="", start_date="", end_date="", **kwargs):
        df = pd.DataFrame([{
            "ts_code": ts_code,
            "trade_date": end_date or "20260703",
            "buy_sm_vol": 1000,
            "sell_sm_vol": 800,
            "buy_md_vol": 2000,
            "sell_md_vol": 1800,
        }])
        return df

    mock.moneyflow = MagicMock(side_effect=_mock_moneyflow)

    # ── limit_list ──
    mock.limit_list = MagicMock(return_value=pd.DataFrame())

    # ── top_list ──
    mock.top_list = MagicMock(return_value=pd.DataFrame())

    return mock


# ============================================================
#  Fixture: 模拟 DB Manager
# ============================================================

@pytest.fixture
def mock_db_manager():
    """Mock scripts.utils.db_manager.get_daily_price_db_first

    返回最近的样本行情数据（降序，60行）
    """
    with patch("scripts.utils.db_manager.get_daily_price_db_first") as mock:
        def _side_effect(ts_code="", days_back=60, caller="test"):
            n = min(days_back, 60)
            base_close = 15.0
            closes = base_close + np.cumsum(np.random.default_rng(42).normal(0, 0.3, n))
            dates = _gen_trade_dates(n)
            df = pd.DataFrame({
                "ts_code": [ts_code] * n,
                "trade_date": dates,
                "open": closes * 0.99,
                "high": closes * 1.02,
                "low": closes * 0.98,
                "close": closes,
                "vol": np.random.default_rng(42).poisson(10000, n),
                "amount": closes * np.random.default_rng(42).poisson(10000, n),
                "pct_chg": np.random.default_rng(42).normal(0, 1.5, n),
            })
            return df.sort_values("trade_date", ascending=False).reset_index(drop=True)
        mock.side_effect = _side_effect
        yield mock


# ============================================================
#  样本数据：已知的 MACD/KDJ/RSI 计算结果
# ============================================================

@pytest.fixture
def sample_price_data() -> pd.DataFrame:
    """60行升序列的行情数据，用于验证技术指标计算

    数据特点：
    - 价格从 10 缓慢上涨到 ~16（用于 MACD 金叉）
    - 中间有一次快速下跌后反弹（用于 KDJ 超卖）
    - 末尾有连续3天小幅下跌（用于检查连跌检测）
    """
    n = 60
    base = 10.0
    rng = np.random.default_rng(42)

    # 构造趋势：先涨→急跌→再涨→震荡
    trend = (
        list(np.linspace(0, 5, 20)) +        # 前20天涨5块
        list(np.linspace(5, -2, 10)) +        # 中10天跌7块
        list(np.linspace(-2, 4, 15)) +        # 再15天涨6块
        list(np.linspace(4, 2, 15))           # 最后15天微跌
    )
    close = [base + t + rng.normal(0, 0.2) for t in trend[:n]]

    dates = []
    import datetime
    d = datetime.date(2026, 7, 7)
    while len(dates) < n:
        if d.weekday() < 5:
            dates.append(d.strftime("%Y%m%d"))
        d -= datetime.timedelta(days=1)
    dates.reverse()

    df = pd.DataFrame({
        "ts_code": ["000001.SZ"] * n,
        "trade_date": dates[:n],
        "open": [round(c * 0.99, 2) for c in close],
        "high": [round(c * 1.03, 2) for c in close],
        "low": [round(c * 0.97, 2) for c in close],
        "close": [round(c, 2) for c in close],
        "vol": [10000 + i * 200 for i in range(n)],
        "amount": [round(c * (10000 + i * 200), 0) for i, c in enumerate(close)],
        "pct_chg": [0.0] + [round((close[i] - close[i-1]) / close[i-1] * 100, 2)
                            for i in range(1, n)],
        "change": [0.0] + [round(close[i] - close[i-1], 2) for i in range(1, n)],
    })
    # 升序排列（technical_analysis 需要升序）
    return df.sort_values("trade_date", ascending=True).reset_index(drop=True)


@pytest.fixture
def sample_report_content() -> str:
    """模拟一份决策报告的 Markdown 内容（用于测试规则提取）"""
    return """# 投资决策报告 — 2026-07-07

## 一、大盘技术面

| 指数 | 收盘 | 涨跌幅 | 技术信号 |
|:----|:----:|:------:|:---------|
| 上证指数 | 4041.24 | **-0.06%** | KDJ金叉但MACD空头，RSI 45.9偏弱 |
| 深证成指 | 15416.8 | **-1.16%** | 空头排列，OBV量能下降 |
| 创业板指 | 3948.86 | **-1.77%** | 空头排列，OBV量能下降 |
| 科创50 | 1996.1 | **+1.04%** | MACD多头+KDJ金叉+RSI 57偏强 |

> **大盘环境评分: 45/100** — 弱势震荡

## 二、风险检查

| 风险项 | 等级 | 说明 |
|:------|:----:|:-----|
| 总仓位超限 | 🔴 CRITICAL | 当前100% → 熊市上限50% |
| 龙净环保仓位 | 🔴 超限 | 36% → 单票上限10% |
| 华友钴业移动止损 | 🟡 触发 | 从高点回撤33.8% |

| 持仓 | 代码 | 盈亏 | 状态 |
|:----|:----|:---:|:-----|
| 黄金ETF华安 | 518880.SH | **-8.0%** | 超-7%止损线 |
| 中材国际 | 600970.SH | **-16.3%** | 超-7%止损线 |
| 宏川智慧 | 002930.SZ | **-19.3%** | 超-7%止损线 |

## 三、执行方案

| 持仓 | 代码 | 数量 | 理由 |
|:----|:----|:----:|:-----|
| **黄金ETF华安** | 518880.SH | **全仓** | 触发止损 |
| **华友钴业** | 603799.SH | **卖1/2** | 止盈+34% |

### 今日不买入
> 大盘环境评分45（弱势），不追加新头寸。

## 四、今日热点

| 维度 | 内容 |
|:----|:------|
| 最强板块 | **半导体/存储芯片**（长电科技+5.44%） |
| 游资方向 | 华天科技(首板)、有研新材(首板) |
| 宏观 | 美联储6月会议纪要、存储芯片涨价 |
"""


# ============================================================
#  工具函数
# ============================================================

def _gen_trade_dates(n: int) -> list[str]:
    """生成 n 个有序交易日 YYYYMMDD（向前推）"""
    import datetime
    dates = []
    d = datetime.date(2026, 7, 7)
    while len(dates) < n:
        if d.weekday() < 5:  # 工作日
            dates.append(d.strftime("%Y%m%d"))
        d -= datetime.timedelta(days=1)
    return dates


# ============================================================
#  自动跳过需要真实 Tushare Token 的测试
# ============================================================

def pytest_configure(config):
    """添加自定义标记"""
    config.addinivalue_line("markers", "needs_tushare: 需要真实 Tushare Token，默认跳过")
    config.addinivalue_line("markers", "slow: 执行时间较长的测试")


def pytest_collection_modifyitems(config, items):
    """跳过需要真实 Tushare 的测试"""
    for item in items:
        if "needs_tushare" in item.keywords:
            item.add_marker(pytest.mark.skip(reason="需要真实 Tushare Token"))
