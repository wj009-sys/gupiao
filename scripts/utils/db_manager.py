"""
本地 SQLite 股票数据库管理器 — 供所有 Agent 统一读写

数据库文件: data/stocks.db
依赖: Python 内置 sqlite3（零额外依赖）

用法:
    from scripts.utils.db_manager import DatabaseManager
    db = DatabaseManager()
    df = db.get_daily_price("000001.SZ", "20260101", "20260630")
    db.upsert_daily_price(df, asset_type='E')

D3异常处理表:
| 触发条件 | 一线修复 | 仍失败兜底 |
|---------|---------|-----------|
| 数据库文件所在目录不可写 | 检查data/目录权限 | 返回空DataFrame/False，调用方降级到API直调 |
| 写入时类型错误（numpy/pandas类型） | 自动转换numpy类型为Python原生类型 | 跳过该行，打印警告 |
| upsert时主键冲突 | INSERT OR REPLACE自动处理 | 记录日志，覆盖旧数据 |
| 读取时表不存在 | 自动执行建表SQL | 返回空DataFrame |
| 数据库文件损坏 | 删除旧文件重建 | 报错提示运行migrate_to_db.py恢复 |
| DataFrame列名与表列名不匹配 | 自动映射常见别名(pct_chg->pct_chg等) | 跳过不存在的列 |

D4 CHECKPOINT:
- CP1-连接检查：每个方法开头检查conn是否存在，不存在则自动连接
- CP2-表存在检查：每个写操作前执行CREATE TABLE IF NOT EXISTS
- CP3-空DataFrame检查：upsert前检查df.empty，空则跳过不报错
- CP4-数据完整性：upsert完成后日志输出写入行数

D9反例：
- 不要硬编码数据库路径（使用项目根目录 + data/stocks.db）
- 不要假设连接一直有效（每次操作前检查，支持自动重连）
- 不要忽略SQL注入（使用参数化查询，不用字符串拼接）
- 不要在循环中逐行INSERT（使用executemany批量写入）
- 不要不处理numpy类型（pandas DataFrame中的numpy.int64/float64需转为Python原生类型）
"""

import os
import sys
import sqlite3
import json
import pandas as pd
import numpy as np
from datetime import datetime
from typing import Optional, List, Dict, Any

# 项目根目录（向上两级：scripts/utils -> scripts -> 根目录）
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DEFAULT_DB_PATH = os.path.join(PROJECT_ROOT, "data", "stocks.db")


# ============================================================
#  建表 SQL（23张表）
# ============================================================

CREATE_TABLES_SQL = [
    # 1. stock_basic — 股票基础信息
    """
    CREATE TABLE IF NOT EXISTS stock_basic (
        ts_code     TEXT PRIMARY KEY,
        name        TEXT,
        market      TEXT,
        industry    TEXT,
        list_status TEXT DEFAULT 'L',
        list_date   TEXT,
        updated_at  TEXT
    )
    """,

    # 2. daily_price — 日线行情（核心表）
    # ⚠️ data_cleaner.py 在运行时还会 ALTER TABLE 添加 8 个扩展列：
    #   adj_factor_val/adj_close_f/adj_open_f/adj_high_f/adj_low_f/adj_close_b/price_limit_flag/price_limit_note
    # 建表语句保持核心 12 列，扩展列由 data_cleaner.py 动态添加
    """
    CREATE TABLE IF NOT EXISTS daily_price (
        ts_code     TEXT NOT NULL,
        trade_date  TEXT NOT NULL,
        asset_type  TEXT DEFAULT 'E',
        open        REAL,
        high        REAL,
        low         REAL,
        close       REAL,
        pre_close   REAL,
        change      REAL,
        pct_chg     REAL,
        vol         REAL,
        amount      REAL,
        PRIMARY KEY (ts_code, trade_date)
    )
    """,

    # 3. daily_basic — 每日基本面
    """
    CREATE TABLE IF NOT EXISTS daily_basic (
        ts_code         TEXT NOT NULL,
        trade_date      TEXT NOT NULL,
        pe              REAL,
        pe_ttm          REAL,
        pb              REAL,
        total_mv        REAL,
        circ_mv         REAL,
        turnover_rate   REAL,
        volume_ratio    REAL,
        PRIMARY KEY (ts_code, trade_date)
    )
    """,

    # 4. fina_indicator — 财务报表指标（季度）
    """
    CREATE TABLE IF NOT EXISTS fina_indicator (
        ts_code             TEXT NOT NULL,
        end_date            TEXT NOT NULL,      -- 报告期 YYYYMMDD
        revenue             REAL,               -- 营业收入（元）
        profit_dedt         REAL,               -- 扣非净利润（元）
        roe                 REAL,               -- ROE %
        roa                 REAL,               -- ROA %
        grossprofit_margin  REAL,               -- 毛利率 %
        debt_to_assets      REAL,               -- 资产负债率 %
        current_ratio       REAL,               -- 流动比率
        revenue_yoy         REAL,               -- 营收同比增长率 %（废弃列，Tushare无此字段，用 or_yoy）
        profit_dedt_yoy     REAL,               -- 净利同比增长率 %
        or_yoy              REAL,               -- 营收同比增长率 %（备用名）
        updated_at          TEXT,
        PRIMARY KEY (ts_code, end_date)
    )
    """,

    # 5. dividend — 分红送转配股
    """
    CREATE TABLE IF NOT EXISTS dividend (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        ts_code         TEXT NOT NULL,
        end_date        TEXT NOT NULL,      -- 报告期 YYYYMMDD
        ann_date        TEXT,               -- 公告日期
        div_proc        TEXT,               -- 进度: 预案/股东大会通过/实施
        stk_div         REAL,               -- 每股送股
        stk_bo_rate     REAL,               -- 每股转增股本
        stk_co_rate     REAL,               -- 每股配股比例
        cash_div        REAL,               -- 每股现金分红(税前)
        cash_div_tax    REAL,               -- 每股现金分红(税后)
        record_date     TEXT,               -- 股权登记日
        ex_date         TEXT,               -- 除权除息日
        pay_date        TEXT,               -- 派息日
        div_listdate    TEXT,               -- 红股上市日
        imp_ann_date    TEXT,               -- 实施公告日
        base_share      REAL,               -- 基准股本(万股)
        updated_at      TEXT,
        UNIQUE(ts_code, end_date, div_proc)
    )
    """,

    # 6. adj_factor — 复权因子（Tushare提供，用于计算前复权价）
    """
    CREATE TABLE IF NOT EXISTS adj_factor (
        ts_code     TEXT NOT NULL,
        trade_date  TEXT NOT NULL,
        adj_factor  REAL NOT NULL,           -- 后复权因子
        PRIMARY KEY (ts_code, trade_date)
    )
    """,

    # 7. daily_indicator — 技术指标（本地计算）
    # ⚠️ 建表语句必须与 DAILY_INDICATOR_COLS 完全一致
    """
    CREATE TABLE IF NOT EXISTS daily_indicator (
        ts_code           TEXT NOT NULL,
        trade_date        TEXT NOT NULL,
        macd              REAL,
        macd_signal       REAL,
        macd_diff         REAL,
        macd_golden_cross INTEGER DEFAULT 0,
        macd_death_cross  INTEGER DEFAULT 0,
        kdj_k             REAL,
        kdj_d             REAL,
        kdj_j             REAL,
        kdj_golden_cross  INTEGER DEFAULT 0,
        rsi_14            REAL,
        rsi_oversold      INTEGER DEFAULT 0,
        rsi_overbought    INTEGER DEFAULT 0,
        boll_upper        REAL,
        boll_mid          REAL,
        boll_lower        REAL,
        boll_width        REAL,
        boll_break_upper  INTEGER DEFAULT 0,
        boll_break_lower  INTEGER DEFAULT 0,
        obv               REAL,
        obv_ma20          REAL,
        obv_trend         TEXT,
        obv_divergence    TEXT,
        ma_5              REAL,
        ma_10             REAL,
        ma_20             REAL,
        ma_60             REAL,
        PRIMARY KEY (ts_code, trade_date)
    )
    """,

    # 8. moneyflow_hsgt — 北向资金
    """
    CREATE TABLE IF NOT EXISTS moneyflow_hsgt (
        trade_date  TEXT PRIMARY KEY,
        north_money REAL,
        south_money REAL,
        hgt         REAL,
        sgt         REAL,
        north_net   REAL
    )
    """,

    # 9. ths_daily — 概念板块每日
    """
    CREATE TABLE IF NOT EXISTS ths_daily (
        ts_code     TEXT NOT NULL,
        trade_date  TEXT NOT NULL,
        name        TEXT,
        pct_chg     REAL,
        strength    REAL,
        anomaly     INTEGER DEFAULT 0,
        PRIMARY KEY (ts_code, trade_date)
    )
    """,

    # 10. portfolio_snapshot — 持仓快照
    """
    CREATE TABLE IF NOT EXISTS portfolio_snapshot (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        snap_date     TEXT NOT NULL,
        ts_code       TEXT NOT NULL,
        name          TEXT,
        asset_type    TEXT,
        cost_price    REAL,
        current_price REAL,
        shares        INTEGER,
        market_value  REAL,
        pnl_pct       REAL,
        position_pct  REAL,
        UNIQUE(snap_date, ts_code)
    )
    """,

    # 11. watchlist — 自选股
    """
    CREATE TABLE IF NOT EXISTS watchlist (
        ts_code     TEXT PRIMARY KEY,
        name        TEXT,
        sector      TEXT,
        added_date  TEXT,
        notes       TEXT
    )
    """,

    # 12. report_log — 报告运行日志
    """
    CREATE TABLE IF NOT EXISTS report_log (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        report_date TEXT NOT NULL,
        agent_id    TEXT NOT NULL,
        status      TEXT DEFAULT 'ok',
        file_path   TEXT,
        error_msg   TEXT,
        created_at  TEXT DEFAULT (datetime('now','localtime')),
        UNIQUE(report_date, agent_id)
    )
    """,

    # 13. moneyflow_mkt — 大盘资金流向（需 Tushare 2000+积分权限）
    """
    CREATE TABLE IF NOT EXISTS moneyflow_mkt (
        trade_date      TEXT NOT NULL,
        ts_code         TEXT NOT NULL,
        net_amount      REAL,
        buy_elg_amount  REAL,
        sell_elg_amount REAL,
        buy_lg_amount   REAL,
        sell_lg_amount  REAL,
        buy_md_amount   REAL,
        sell_md_amount  REAL,
        buy_sm_amount   REAL,
        sell_sm_amount  REAL,
        PRIMARY KEY (trade_date, ts_code)
    )
    """,

    # 14. margin — 融资融券（沪深两市）
    """
    CREATE TABLE IF NOT EXISTS margin (
        trade_date  TEXT NOT NULL,
        exchange    TEXT NOT NULL,
        rzye        REAL,
        rqye        REAL,
        rzrqye      REAL,
        rzmre       REAL,
        rqyl        REAL,
        rzche       REAL,
        rqchl       REAL,
        rzjmre      REAL,
        rqjmyl      REAL,
        PRIMARY KEY (trade_date, exchange)
    )
    """,

    # 15. decision_log — 决策审计日志（TradingAgents借鉴自SQLite持久化）
    """
    CREATE TABLE IF NOT EXISTS decision_log (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        log_date    TEXT NOT NULL,
        decision_type TEXT NOT NULL,
        action      TEXT NOT NULL,
        summary     TEXT,
        reasoning   TEXT,
        risk_level  TEXT,
        conflict_count INTEGER DEFAULT 0,
        rework_count   INTEGER DEFAULT 0,
        sources     TEXT,
        created_at  TEXT DEFAULT (datetime('now','localtime'))
    )
    """,

    # 16. policy_events — 政策事件数据库（Agent8政策分析师使用）
    """
    CREATE TABLE IF NOT EXISTS policy_events (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        event_date    TEXT NOT NULL,
        title         TEXT,
        content       TEXT,
        source        TEXT,
        category      TEXT,          -- 宏观调控/产业政策/监管动态/税收政策
        impact_sector TEXT,          -- 影响行业/板块
        impact_score  INTEGER,       -- -5(重大利空) ~ 0(中性) ~ +5(重大利好)
        related_codes TEXT,          -- 关联股票代码(逗号分隔)
        url           TEXT,
        created_at    TEXT DEFAULT (datetime('now','localtime'))
    )
    """,

    # 17. dragon_tiger_detail — 龙虎榜机构席位明细（Agent9游资追踪师使用）
    """
    CREATE TABLE IF NOT EXISTS dragon_tiger_detail (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        trade_date    TEXT NOT NULL,
        ts_code       TEXT NOT NULL,
        name          TEXT,
        close         REAL,
        pct_chg       REAL,
        turnover_ratio REAL,
        amount        REAL,
        buy_amount    REAL,
        sell_amount   REAL,
        net_amount    REAL,
        buy_seats     TEXT,           -- 买入席位JSON
        sell_seats    TEXT,           -- 卖出席位JSON
        reason_type   TEXT,           -- 上榜原因
        created_at    TEXT DEFAULT (datetime('now','localtime'))
    )
    """,

    # 18. hot_money_seats — 游资席位跟踪（Agent9游资追踪师使用）
    """
    CREATE TABLE IF NOT EXISTS hot_money_seats (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        trade_date    TEXT NOT NULL,
        seat_name     TEXT NOT NULL,
        seat_type     TEXT,           -- 游资/机构/量化/散户
        total_buy     REAL,
        total_sell    REAL,
        net_amount    REAL,
        active_stocks INTEGER,       -- 操作股票数
        style         TEXT,           -- 打板/趋势/低吸
        created_at    TEXT DEFAULT (datetime('now','localtime'))
    )
    """,

    # 19. lockup_schedule — 限售股解禁日历（Agent3风控扩展使用）
    """
    CREATE TABLE IF NOT EXISTS lockup_schedule (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        ts_code       TEXT NOT NULL,
        name          TEXT,
        unlock_date   TEXT,           -- 解禁日期
        unlock_volume REAL,           -- 解禁数量(万股)
        unlock_ratio  REAL,           -- 解禁占流通股比(%)
        market_value  REAL,           -- 解禁市值(万元)
        holder_name   TEXT,           -- 解禁股东
        lockup_type   TEXT,           -- 首发/定增/股权激励
        created_at    TEXT DEFAULT (datetime('now','localtime'))
    )
    """,

    # 20. margin_detail — 个股融资融券明细
    """
    CREATE TABLE IF NOT EXISTS margin_detail (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        ts_code       TEXT NOT NULL,
        trade_date    TEXT NOT NULL,
        rzye          REAL,           -- 融资余额(元)
        rqye          REAL,           -- 融券余额(元)
        rzmre         REAL,           -- 融资买入额(元)
        rqmcl         REAL,           -- 融券卖出量(股)
        rzrqye        REAL,           -- 融资融券余额(元)
        UNIQUE(ts_code, trade_date)
    )
    """,

    # 21. moneyflow_stock — 个股资金流向
    """
    CREATE TABLE IF NOT EXISTS moneyflow_stock (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        ts_code         TEXT NOT NULL,
        trade_date      TEXT NOT NULL,
        net_amount      REAL,           -- 净流入(万元)
        buy_lg_amount   REAL,           -- 大单买入(万元)
        sell_lg_amount  REAL,           -- 大单卖出(万元)
        buy_md_amount   REAL,           -- 中单买入(万元)
        sell_md_amount  REAL,           -- 中单卖出(万元)
        buy_sm_amount   REAL,           -- 小单买入(万元)
        sell_sm_amount  REAL,           -- 小单卖出(万元)
        net_lg_amount   REAL,           -- 大单净额(万元)
        buy_elg_amount  REAL,           -- 超大单买入(万元)
        sell_elg_amount REAL,           -- 超大单卖出(万元)
        UNIQUE(ts_code, trade_date)
    )
    """,

    # 22. fund_basic — ETF基金基础信息
    """
    CREATE TABLE IF NOT EXISTS fund_basic (
        ts_code     TEXT PRIMARY KEY,
        name        TEXT,
        fund_type   TEXT,
        invest_type TEXT,
        management  TEXT,
        custodian   TEXT,
        found_date  TEXT,
        list_date   TEXT,
        delist_date TEXT,
        m_fee       REAL,
        c_fee       REAL,
        issue_amount REAL,
        benchmark   TEXT,
        status      TEXT DEFAULT 'L',
        market      TEXT DEFAULT 'E',
        updated_at  TEXT
    )
    """,

    # 23. index_basic — 指数基础信息
    """
    CREATE TABLE IF NOT EXISTS index_basic (
        ts_code     TEXT PRIMARY KEY,
        name        TEXT,
        market      TEXT,
        publisher   TEXT,
        category    TEXT,
        base_date   TEXT,
        base_point  REAL,
        list_date   TEXT,
        updated_at  TEXT
    )
    """,
]

CREATE_INDEXES_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_daily_date ON daily_price(trade_date)",
    "CREATE INDEX IF NOT EXISTS idx_daily_asset ON daily_price(asset_type, trade_date)",
    "CREATE INDEX IF NOT EXISTS idx_daily_tscode ON daily_price(ts_code)",
    "CREATE INDEX IF NOT EXISTS idx_indicator_date ON daily_indicator(trade_date)",
    "CREATE INDEX IF NOT EXISTS idx_indicator_tscode ON daily_indicator(ts_code)",
    "CREATE INDEX IF NOT EXISTS idx_basic_date ON daily_basic(trade_date)",
    "CREATE INDEX IF NOT EXISTS idx_ths_date ON ths_daily(trade_date)",
    "CREATE INDEX IF NOT EXISTS idx_portfolio_date ON portfolio_snapshot(snap_date)",
    "CREATE INDEX IF NOT EXISTS idx_report_date ON report_log(report_date)",
    "CREATE INDEX IF NOT EXISTS idx_fina_tscode ON fina_indicator(ts_code)",
    "CREATE INDEX IF NOT EXISTS idx_fina_date ON fina_indicator(end_date)",
    "CREATE INDEX IF NOT EXISTS idx_div_tscode ON dividend(ts_code)",
    "CREATE INDEX IF NOT EXISTS idx_div_exdate ON dividend(ex_date)",
    "CREATE INDEX IF NOT EXISTS idx_adj_date ON adj_factor(trade_date)",
    "CREATE INDEX IF NOT EXISTS idx_moneyflow_mkt_date ON moneyflow_mkt(trade_date)",
    "CREATE INDEX IF NOT EXISTS idx_margin_date ON margin(trade_date)",
    "CREATE INDEX IF NOT EXISTS idx_decision_date ON decision_log(log_date)",
    "CREATE INDEX IF NOT EXISTS idx_decision_type ON decision_log(decision_type)",
    "CREATE INDEX IF NOT EXISTS idx_fund_status ON fund_basic(status)",
    "CREATE INDEX IF NOT EXISTS idx_fund_type ON fund_basic(fund_type)",
    "CREATE INDEX IF NOT EXISTS idx_index_market ON index_basic(market)",
    # 达尔文12.0 新增索引
    "CREATE INDEX IF NOT EXISTS idx_moneyflow_stock_date ON moneyflow_stock(trade_date)",
    "CREATE INDEX IF NOT EXISTS idx_margin_detail_date ON margin_detail(trade_date)",
    "CREATE INDEX IF NOT EXISTS idx_dragon_tiger_date ON dragon_tiger_detail(trade_date)",
    "CREATE INDEX IF NOT EXISTS idx_lockup_date ON lockup_schedule(unlock_date)",
    # 达尔文14.0 新增索引（实际已存在于DB中，补注册到集中定义）
    "CREATE INDEX IF NOT EXISTS idx_adj_tscode ON adj_factor(ts_code)",
    "CREATE INDEX IF NOT EXISTS idx_dragon_tscode ON dragon_tiger_detail(ts_code)",
    "CREATE INDEX IF NOT EXISTS idx_hot_money_seat_name ON hot_money_seats(seat_name)",
    "CREATE INDEX IF NOT EXISTS idx_lockup_tscode ON lockup_schedule(ts_code)",
    "CREATE INDEX IF NOT EXISTS idx_policy_category ON policy_events(category)",
    "CREATE INDEX IF NOT EXISTS idx_policy_date ON policy_events(event_date)",
    "CREATE INDEX IF NOT EXISTS idx_policy_sector ON policy_events(impact_sector)",
    "CREATE INDEX IF NOT EXISTS idx_portfolio_tscode ON portfolio_snapshot(ts_code)",
    "CREATE INDEX IF NOT EXISTS idx_seat_date ON hot_money_seats(trade_date)",
    "CREATE INDEX IF NOT EXISTS idx_stock_industry ON stock_basic(industry)",
    "CREATE INDEX IF NOT EXISTS idx_stock_market ON stock_basic(market)",
]


# ============================================================
#  列名映射（Tushare API → 数据库列名）
# ============================================================

# daily_price 列映射：处理不同数据源的列名差异
DAILY_PRICE_COL_MAP = {
    "ts_code": "ts_code",
    "trade_date": "trade_date",
    "open": "open",
    "high": "high",
    "low": "low",
    "close": "close",
    "pre_close": "pre_close",
    "change": "change",
    "pct_chg": "pct_chg",
    "pct_change": "pct_chg",  # fetch_all.py 用的别名
    "vol": "vol",
    "volume": "vol",          # technical_analysis.py 用的别名
    "amount": "amount",
}

DAILY_BASIC_COL_MAP = {
    "ts_code": "ts_code",
    "trade_date": "trade_date",
    "pe": "pe",
    "pe_ttm": "pe_ttm",
    "pb": "pb",
    "total_mv": "total_mv",
    "circ_mv": "circ_mv",
    "turnover_rate": "turnover_rate",
    "volume_ratio": "volume_ratio",
}

DAILY_INDICATOR_COLS = [
    "ts_code", "trade_date",
    "macd", "macd_signal", "macd_diff", "macd_golden_cross", "macd_death_cross",
    "kdj_k", "kdj_d", "kdj_j", "kdj_golden_cross",
    "rsi_14", "rsi_oversold", "rsi_overbought",
    "boll_upper", "boll_mid", "boll_lower", "boll_width",
    "boll_break_upper", "boll_break_lower",
    "obv", "obv_ma20", "obv_trend", "obv_divergence",
    "ma_5", "ma_10", "ma_20", "ma_60",
]

THS_DAILY_COL_MAP = {
    "ts_code": "ts_code",
    "name": "name",
    "pct_chg": "pct_chg",
    "strength": "strength",
    "anomaly": "anomaly",
}

# fina_indicator 列映射：Tushare API 字段名 → 数据库列名
# Tushare 字段: dt_profit_yoy = 净利润同比增长率，or_yoy = 营收同比增长率
FINA_INDICATOR_COL_MAP = {
    "ts_code": "ts_code",
    "end_date": "end_date",
    "revenue": "revenue",           # 营业收入（元）
    "profit_dedt": "profit_dedt",   # 扣非净利润（元）
    "roe": "roe",
    "roa": "roa",
    "grossprofit_margin": "grossprofit_margin",
    "debt_to_assets": "debt_to_assets",
    "current_ratio": "current_ratio",
    "or_yoy": "or_yoy",             # 营收同比增长率 %
    "dt_netprofit_yoy": "profit_dedt_yoy",  # 扣非净利同比增长率 %
    "op_income": "revenue",         # 营业总收入（元）→ 营业收入
}

# 数据库fina_indicator列（用于自动检测存在的列）
FINA_INDICATOR_DB_COLS = [
    "ts_code", "end_date", "revenue", "profit_dedt", "roe", "roa",
    "grossprofit_margin", "debt_to_assets", "current_ratio",
    "profit_dedt_yoy", "or_yoy",
]


# ============================================================
#  工具函数
# ============================================================

def _to_native(val):
    """将 numpy 类型转换为 Python 原生类型，兼容 None/NaN"""
    if val is None:
        return None
    try:
        if isinstance(val, (np.integer,)):
            return int(val)
        if isinstance(val, (np.floating,)):
            if np.isnan(val) or np.isinf(val):
                return None
            return float(val)
        if isinstance(val, (np.bool_,)):
            return bool(val)
        if isinstance(val, (np.ndarray,)):
            return val.tolist()
        if isinstance(val, pd.Timestamp):
            return val.strftime("%Y%m%d")
    except Exception as e:
        # 类型转换失败时，回退到安全字符串，避免非预期类型进入 SQLite
        print(f"[DB] _to_native 类型转换失败: {e}, val type={type(val)}")
        return str(val) if val is not None else ''


def _safe_float(val, default=None):
    """安全转换为 float"""
    try:
        v = float(val)
        if np.isnan(v) or np.isinf(v):
            return default
        return v
    except (TypeError, ValueError):
        return default


def _safe_int(val, default=None):
    """安全转换为 int"""
    try:
        return int(val)
    except (TypeError, ValueError):
        return default

# ============================================================
#  DatabaseManager 类
# ============================================================

class DatabaseManager:
    """本地 SQLite 股票数据库管理器"""

    def __init__(self, db_path: str = None):
        """
        初始化数据库管理器

        Args:
            db_path: 数据库文件路径，默认 data/stocks.db
        """
        self.db_path = db_path or DEFAULT_DB_PATH
        self.conn: Optional[sqlite3.Connection] = None
        self._auto_connect()

    def _auto_connect(self):
        """自动建立数据库连接并创建表"""
        try:
            # 确保目录存在
            db_dir = os.path.dirname(self.db_path)
            if db_dir and not os.path.exists(db_dir):
                os.makedirs(db_dir, exist_ok=True)

            self.conn = sqlite3.connect(self.db_path)
            self.conn.execute("PRAGMA journal_mode=WAL")      # 提高并发写入性能
            self.conn.execute("PRAGMA foreign_keys=OFF")       # 简化操作
            self._create_tables()
            print(f"[DB] 数据库已连接: {self.db_path}")
        except Exception as e:
            print(f"[DB] 连接失败: {e}")
            self.conn = None

    def _ensure_conn(self) -> bool:
        """确保连接有效，失效时自动重连"""
        if self.conn is None:
            self._auto_connect()
        if self.conn is None:
            return False
        try:
            self.conn.execute("SELECT 1")
            return True
        except Exception as e:
            print(f"  [WARN] DB连接检查失败: {e}")
            self._auto_connect()
            return self.conn is not None

    def _create_tables(self):
        """创建所有表（如不存在）"""
        if not self.conn:
            return
        try:
            cur = self.conn.cursor()
            for sql in CREATE_TABLES_SQL:
                cur.execute(sql)
            for sql in CREATE_INDEXES_SQL:
                try:
                    cur.execute(sql)
                except Exception as e:
                    print(f'  [DB] 建索引失败: {e}')  # 索引创建失败不阻塞主流程
            # ── Schema 迁移: 为已有表添加新列 ──
            _migrations = [
                "ALTER TABLE moneyflow_stock ADD COLUMN buy_elg_amount REAL",
                "ALTER TABLE moneyflow_stock ADD COLUMN sell_elg_amount REAL",
            ]
            for mm in _migrations:
                try:
                    cur.execute(mm)
                except Exception:
                    pass  # 列已存在时忽略
            self.conn.commit()
        except Exception as e:
            print(f"[DB] 建表失败: {e}")

    def close(self):
        """关闭数据库连接"""
        if self.conn:
            try:
                self.conn.close()
            except Exception as e:
                print(f'[DB] 关闭连接失败: {e}')
            self.conn = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False

    # ============================================================
    #  写入方法
    # ============================================================

    def upsert_daily_price(self, df: pd.DataFrame, asset_type: str = 'E') -> int:
        """
        批量写入日线行情（INSERT OR REPLACE）

        Args:
            df: 包含 OHLCV 数据的 DataFrame，需含 ts_code, trade_date 列
            asset_type: E=股票, I=指数, F=基金/ETF

        Returns:
            写入行数
        """
        if not self._ensure_conn():
            return 0
        if df is None or df.empty:
            return 0

        columns = ["ts_code", "trade_date", "open", "high", "low", "close",
                    "pre_close", "change", "pct_chg", "vol", "amount"]
        rows = []
        for _, row in df.iterrows():
            r = {
                "ts_code": str(row.get("ts_code", "")),
                "trade_date": str(row.get("trade_date", "")),
                "open": _safe_float(row.get("open")),
                "high": _safe_float(row.get("high")),
                "low": _safe_float(row.get("low")),
                "close": _safe_float(row.get("close")),
                "pre_close": _safe_float(row.get("pre_close")),
                "change": _safe_float(row.get("change")),
                "pct_chg": _safe_float(row.get("pct_chg", row.get("pct_change"))),
                "vol": _safe_float(row.get("vol", row.get("volume"))),
                "amount": _safe_float(row.get("amount")),
            }
            if not r["ts_code"] or not r["trade_date"]:
                continue
            rows.append(r)

        if not rows:
            return 0

        try:
            cur = self.conn.cursor()
            sql = f"""INSERT OR REPLACE INTO daily_price
                (ts_code, trade_date, asset_type, open, high, low, close,
                 pre_close, change, pct_chg, vol, amount)
                VALUES (?, ?, '{asset_type}', ?, ?, ?, ?, ?, ?, ?, ?, ?)"""
            cur.executemany(sql, [
                (r["ts_code"], r["trade_date"], r["open"], r["high"], r["low"],
                 r["close"], r["pre_close"], r["change"], r["pct_chg"],
                 r["vol"], r["amount"])
                for r in rows
            ])
            self.conn.commit()
            print(f"[DB] daily_price 写入 {len(rows)} 行 (asset_type={asset_type})")
            return len(rows)
        except Exception as e:
            print(f"[DB] daily_price 写入失败: {e}")
            return 0

    def upsert_daily_basic(self, df: pd.DataFrame) -> int:
        """写入每日基本面指标"""
        if not self._ensure_conn():
            return 0
        if df is None or df.empty:
            return 0

        rows = []
        for _, row in df.iterrows():
            r = {
                "ts_code": str(row.get("ts_code", "")),
                "trade_date": str(row.get("trade_date", "")),
                "pe": _safe_float(row.get("pe")),
                "pe_ttm": _safe_float(row.get("pe_ttm")),
                "pb": _safe_float(row.get("pb")),
                "total_mv": _safe_float(row.get("total_mv")),
                "circ_mv": _safe_float(row.get("circ_mv")),
                "turnover_rate": _safe_float(row.get("turnover_rate")),
                "volume_ratio": _safe_float(row.get("volume_ratio")),
            }
            if not r["ts_code"] or not r["trade_date"]:
                continue
            rows.append(r)

        if not rows:
            return 0

        try:
            cur = self.conn.cursor()
            sql = """INSERT OR REPLACE INTO daily_basic
                (ts_code, trade_date, pe, pe_ttm, pb, total_mv, circ_mv,
                 turnover_rate, volume_ratio)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"""
            cur.executemany(sql, [
                (r["ts_code"], r["trade_date"], r["pe"], r["pe_ttm"], r["pb"],
                 r["total_mv"], r["circ_mv"], r["turnover_rate"], r["volume_ratio"])
                for r in rows
            ])
            self.conn.commit()
            print(f"[DB] daily_basic 写入 {len(rows)} 行")
            return len(rows)
        except Exception as e:
            print(f"[DB] daily_basic 写入失败: {e}")
            return 0

    def upsert_dividend(self, df: pd.DataFrame) -> int:
        """
        写入分红送转配股数据（INSERT OR REPLACE）

        Args:
            df: Tushare dividend 接口返回的 DataFrame
                含 ts_code, end_date, div_proc, stk_div, stk_bo_rate,
                stk_co_rate, cash_div, cash_div_tax, record_date, ex_date 等

        Returns:
            写入行数
        """
        if not self._ensure_conn():
            return 0
        if df is None or df.empty:
            return 0

        db_cols = [
            "ts_code", "end_date", "ann_date", "div_proc",
            "stk_div", "stk_bo_rate", "stk_co_rate",
            "cash_div", "cash_div_tax",
            "record_date", "ex_date", "pay_date", "div_listdate", "imp_ann_date",
            "base_share",
        ]
        available_cols = [c for c in db_cols if c in df.columns]
        if "ts_code" not in available_cols:
            print("[DB] dividend: 缺少 ts_code 列，跳过")
            return 0

        try:
            rows = []
            for _, row in df.iterrows():
                vals = []
                for col in available_cols:
                    v = row.get(col)
                    if col in ("ts_code", "end_date", "ann_date", "div_proc",
                               "record_date", "ex_date", "pay_date", "div_listdate",
                               "imp_ann_date"):
                        vals.append(str(v) if v is not None and str(v) != "nan" else None)
                    else:
                        vals.append(_safe_float(v))
                vals.append(datetime.now().strftime("%Y-%m-%d %H:%M"))
                rows.append(tuple(vals))
            available_cols.append("updated_at")

            placeholders = ", ".join(["?" for _ in available_cols])
            cols_str = ", ".join(available_cols)
            sql = f"INSERT OR REPLACE INTO dividend ({cols_str}) VALUES ({placeholders})"

            cur = self.conn.cursor()
            cur.executemany(sql, rows)
            self.conn.commit()
            print(f"[DB] dividend 写入 {len(rows)} 行")
            return len(rows)
        except Exception as e:
            print(f"[DB] dividend 写入失败: {e}")
            return 0

    def get_dividend(self, ts_code: str) -> list:
        """
        获取某只股票的分红送转历史

        Args:
            ts_code: 股票代码

        Returns:
            [{ts_code, end_date, div_proc, stk_div, cash_div, ex_date, ...}, ...]
            按除权日降序排列
        """
        if not self._ensure_conn():
            return []

        try:
            cur = self.conn.cursor()
            cur.execute(
                """SELECT ts_code, end_date, div_proc, stk_div, stk_bo_rate, stk_co_rate,
                          cash_div, cash_div_tax, record_date, ex_date, pay_date
                   FROM dividend WHERE ts_code = ?
                   ORDER BY ex_date DESC""",
                (ts_code,)
            )
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in rows]
        except Exception as e:
            print(f"[DB] get_dividend 失败 ({ts_code}): {e}")
        return []

    def get_recent_dividends(self, days_ahead: int = 60) -> list:
        """
        获取未来N天内即将除权的股票（用于盯盘提醒）

        Args:
            days_ahead: 未来天数

        Returns:
            [{ts_code, cash_div, ex_date, ...}, ...]
        """
        if not self._ensure_conn():
            return []

        today = datetime.now().strftime("%Y%m%d")
        future = (datetime.now() + pd.Timedelta(days=days_ahead)).strftime("%Y%m%d")

        try:
            cur = self.conn.cursor()
            cur.execute(
                """SELECT ts_code, end_date, div_proc, stk_div, cash_div, cash_div_tax,
                          record_date, ex_date, pay_date
                   FROM dividend
                   WHERE ex_date >= ? AND ex_date <= ? AND div_proc = '实施'
                   ORDER BY ex_date ASC""",
                (today, future)
            )
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in rows]
        except Exception as e:
            print(f"[DB] get_recent_dividends 失败: {e}")
        return []

    def upsert_fina_indicator(self, df: pd.DataFrame) -> int:
        """
        写入财务报表指标（季度数据，INSERT OR REPLACE）

        Args:
            df: 含 ts_code, end_date, roe, roa, or_yoy, dt_profit_yoy 等列的 DataFrame
                列名会被 FINA_INDICATOR_COL_MAP 映射到数据库列名

        Returns:
            写入行数
        """
        if not self._ensure_conn():
            return 0
        if df is None or df.empty:
            return 0

        # 映射列名：Tushare API → 数据库列名
        df_renamed = df.rename(columns=FINA_INDICATOR_COL_MAP)
        # 只取数据库存在的列
        available_cols = [c for c in FINA_INDICATOR_DB_COLS if c in df_renamed.columns]
        if "ts_code" not in available_cols or "end_date" not in available_cols:
            print("[DB] fina_indicator: 缺少 ts_code 或 end_date 列，跳过")
            return 0

        try:
            rows = []
            for _, row in df_renamed.iterrows():
                vals = []
                for col in available_cols:
                    v = row.get(col)
                    if col in ("ts_code", "end_date"):
                        vals.append(str(v) if v is not None else None)
                    else:
                        vals.append(_safe_float(v))
                vals.append(datetime.now().strftime("%Y-%m-%d %H:%M"))  # updated_at
                rows.append(tuple(vals))
            available_cols.append("updated_at")

            placeholders = ", ".join(["?" for _ in available_cols])
            cols_str = ", ".join(available_cols)
            sql = f"INSERT OR REPLACE INTO fina_indicator ({cols_str}) VALUES ({placeholders})"

            cur = self.conn.cursor()
            cur.executemany(sql, rows)
            self.conn.commit()
            print(f"[DB] fina_indicator 写入 {len(rows)} 行")
            return len(rows)
        except Exception as e:
            print(f"[DB] fina_indicator 写入失败: {e}")
            return 0

    def get_fina_indicator(self, ts_code: str, latest: bool = True) -> dict:
        """
        获取某只股票最新的财务指标

        Args:
            ts_code: 股票代码
            latest: True=最新一期, False=返回DataFrame

        Returns:
            最新一期数据字典，或空字典
        """
        if not self._ensure_conn():
            return {}

        try:
            cur = self.conn.cursor()
            if latest:
                cur.execute(
                    "SELECT * FROM fina_indicator WHERE ts_code = ? ORDER BY end_date DESC LIMIT 1",
                    (ts_code,)
                )
                row = cur.fetchone()
                if row:
                    cols = [d[0] for d in cur.description]
                    return dict(zip(cols, row))
            else:
                # 返回所有期数据
                cur.execute(
                    "SELECT * FROM fina_indicator WHERE ts_code = ? ORDER BY end_date DESC",
                    (ts_code,)
                )
                rows = cur.fetchall()
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, r)) for r in rows]
        except Exception as e:
            print(f"[DB] get_fina_indicator 失败 ({ts_code}): {e}")
        return {}

    def upsert_adj_factor(self, df: pd.DataFrame) -> int:
        """
        写入复权因子（INSERT OR REPLACE）

        Args:
            df: Tushare adj_factor 接口返回的 DataFrame
                含 ts_code, trade_date, adj_factor

        Returns:
            写入行数
        """
        if not self._ensure_conn():
            return 0
        if df is None or df.empty:
            return 0

        if "adj_factor" not in df.columns:
            print("[DB] adj_factor: 缺少 adj_factor 列，跳过")
            return 0

        try:
            rows = []
            for _, row in df.iterrows():
                ts = str(row.get("ts_code", ""))
                td = str(row.get("trade_date", ""))
                af = _safe_float(row.get("adj_factor"))
                if not ts or not td or af is None:
                    continue
                rows.append((ts, td, af))

            if not rows:
                return 0

            cur = self.conn.cursor()
            cur.executemany(
                "INSERT OR REPLACE INTO adj_factor (ts_code, trade_date, adj_factor) VALUES (?, ?, ?)",
                rows
            )
            self.conn.commit()
            print(f"[DB] adj_factor 写入 {len(rows)} 行")
            return len(rows)
        except Exception as e:
            print(f"[DB] adj_factor 写入失败: {e}")
            return 0

    def get_daily_price_adj(self, ts_code: str, start_date: str = None,
                            end_date: str = None) -> pd.DataFrame:
        """
        获取前复权日线行情

        计算逻辑：前复权价 = close × (latest_adj_factor / 当日adj_factor)
        自动 JOIN daily_price 和 adj_factor 两表

        Args:
            ts_code: 股票代码
            start_date: 起始日期 YYYYMMDD
            end_date: 结束日期 YYYYMMDD

        Returns:
            DataFrame，含 OHLCV + adj_factor + 前复权价(close_adj)等列
        """
        if not self._ensure_conn():
            return pd.DataFrame()

        try:
            # 检查该股票是否有可用的复权因子（>0）
            cur = self.conn.cursor()
            cur.execute(
                "SELECT 1 FROM adj_factor WHERE ts_code = ? AND adj_factor > 0 LIMIT 1",
                (ts_code,)
            )
            if not cur.fetchone():
                # 无复权因子，返回原始价格
                return self.get_daily_price(ts_code, start_date, end_date)

            # CTE + JOIN 查询，计算前复权价（全部参数化，无 f-string 嵌入）
            conditions = ["p.ts_code = ?"]
            params = [ts_code]
            if start_date:
                conditions.append("p.trade_date >= ?")
                params.append(start_date)
            if end_date:
                conditions.append("p.trade_date <= ?")
                params.append(end_date)

            sql = f"""
                WITH latest_af AS (
                    SELECT adj_factor FROM adj_factor
                    WHERE ts_code = ? ORDER BY trade_date DESC LIMIT 1
                )
                SELECT p.ts_code, p.trade_date,
                       ROUND(p.open  / a.adj_factor * COALESCE(NULLIF(la.adj_factor, 0), 1), 4) AS open_adj,
                       ROUND(p.high  / a.adj_factor * COALESCE(NULLIF(la.adj_factor, 0), 1), 4) AS high_adj,
                       ROUND(p.low   / a.adj_factor * COALESCE(NULLIF(la.adj_factor, 0), 1), 4) AS low_adj,
                       ROUND(p.close / a.adj_factor * COALESCE(NULLIF(la.adj_factor, 0), 1), 4) AS close_adj,
                       p.open, p.high, p.low, p.close,
                       p.pre_close, p.change, p.pct_chg,
                       p.vol, p.amount,
                       a.adj_factor,
                       COALESCE(NULLIF(la.adj_factor, 0), 1) AS latest_adj_factor
                FROM daily_price p
                JOIN adj_factor a ON a.ts_code = p.ts_code AND a.trade_date = p.trade_date
                CROSS JOIN latest_af la
                WHERE {' AND '.join(conditions)}
                ORDER BY p.trade_date ASC
            """
            all_params = [ts_code] + params  # CTE param + WHERE params
            return pd.read_sql_query(sql, self.conn, params=all_params)
        except Exception as e:
            print(f"[DB] get_daily_price_adj 失败 ({ts_code}): {e}")
            return self.get_daily_price(ts_code, start_date, end_date)

    def has_adj_factor(self, ts_code: str) -> bool:
        """检查某只股票是否有复权因子数据"""
        if not self._ensure_conn():
            return False
        try:
            cur = self.conn.cursor()
            cur.execute("SELECT 1 FROM adj_factor WHERE ts_code = ? LIMIT 1", (ts_code,))
            return cur.fetchone() is not None
        except Exception as e:
            print(f"  [WARN] has_adj_factor({ts_code}) 失败: {e}")
            return False

    def upsert_daily_indicator(self, df: pd.DataFrame) -> int:
        """写入技术指标（INSERT OR REPLACE）"""
        if not self._ensure_conn():
            return 0
        if df is None or df.empty:
            return 0

        # 自动检测需要写入的列（只写数据库中存在的列）
        available_cols = [c for c in DAILY_INDICATOR_COLS if c in df.columns]
        if "ts_code" not in available_cols or "trade_date" not in available_cols:
            print("[DB] daily_indicator: 缺少 ts_code 或 trade_date 列，跳过")
            return 0

        try:
            # 构建动态SQL
            placeholders = ", ".join(["?" for _ in available_cols])
            cols_str = ", ".join(available_cols)
            sql = f"INSERT OR REPLACE INTO daily_indicator ({cols_str}) VALUES ({placeholders})"

            # 字符串类型列
            STR_COLS = {"ts_code", "trade_date"}
            # 布尔→整数转换列
            BOOL_COLS = {"macd_golden_cross", "macd_death_cross", "kdj_golden_cross"}

            rows = []
            for _, row in df.iterrows():
                vals = []
                for col in available_cols:
                    v = row.get(col)
                    if col in STR_COLS:
                        vals.append(str(v) if v is not None else None)
                    elif col in BOOL_COLS:
                        vals.append(1 if v else 0)
                    else:
                        vals.append(_safe_float(v))
                rows.append(tuple(vals))

            cur = self.conn.cursor()
            cur.executemany(sql, rows)
            self.conn.commit()
            print(f"[DB] daily_indicator 写入 {len(rows)} 行")
            return len(rows)
        except Exception as e:
            print(f"[DB] daily_indicator 写入失败: {e}")
            return 0

    def upsert_moneyflow_hsgt(self, trade_date: str, data: dict) -> bool:
        """写入北向资金流向（单条）"""
        if not self._ensure_conn():
            return False
        if not trade_date or not data:
            return False

        try:
            cur = self.conn.cursor()
            sql = """INSERT OR REPLACE INTO moneyflow_hsgt
                (trade_date, north_money, south_money, hgt, sgt, north_net)
                VALUES (?, ?, ?, ?, ?, ?)"""
            cur.execute(sql, (
                str(trade_date),
                _safe_float(data.get("north_money")),
                _safe_float(data.get("south_money")),
                _safe_float(data.get("hgt")),
                _safe_float(data.get("sgt")),
                _safe_float(data.get("north_net")),
            ))
            self.conn.commit()
            print(f"[DB] moneyflow_hsgt 写入 {trade_date}")
            return True
        except Exception as e:
            print(f"[DB] moneyflow_hsgt 写入失败: {e}")
            return False

    def upsert_moneyflow_stock(self, df: pd.DataFrame) -> int:
        """批量写入个股资金流向（来自 Tushare moneyflow API）

        Tushare API 字段映射（值单位：万元）:
            ts_code, trade_date → DB 同名字段
            net_mf_amount → net_amount
            buy_lg_amount → buy_lg_amount
            sell_lg_amount → sell_lg_amount
            buy_md_amount → buy_md_amount
            sell_md_amount → sell_md_amount
            buy_sm_amount → buy_sm_amount
            sell_sm_amount → sell_sm_amount
            buy_lg_amount - sell_lg_amount → net_lg_amount (计算)
        """
        if not self._ensure_conn():
            return 0
        if df is None or df.empty:
            return 0

        rows = []
        for _, row in df.iterrows():
            ts_code = str(row.get("ts_code", ""))
            trade_date = str(row.get("trade_date", ""))
            if not ts_code or not trade_date:
                continue
            buy_lg = _safe_float(row.get("buy_lg_amount"), 0)
            sell_lg = _safe_float(row.get("sell_lg_amount"), 0)
            rows.append((
                ts_code, trade_date,
                _safe_float(row.get("net_mf_amount"), 0),
                buy_lg, sell_lg,
                _safe_float(row.get("buy_md_amount"), 0),
                _safe_float(row.get("sell_md_amount"), 0),
                _safe_float(row.get("buy_sm_amount"), 0),
                _safe_float(row.get("sell_sm_amount"), 0),
                buy_lg - sell_lg,  # net_lg_amount
                _safe_float(row.get("buy_elg_amount"), None),
                _safe_float(row.get("sell_elg_amount"), None),
            ))

        if not rows:
            return 0

        try:
            cur = self.conn.cursor()
            sql = """INSERT OR IGNORE INTO moneyflow_stock
                (ts_code, trade_date, net_amount,
                 buy_lg_amount, sell_lg_amount,
                 buy_md_amount, sell_md_amount,
                 buy_sm_amount, sell_sm_amount,
                 net_lg_amount,
                 buy_elg_amount, sell_elg_amount)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"""
            cur.executemany(sql, rows)
            self.conn.commit()
            return len(rows)
        except Exception as e:
            print(f"[DB] moneyflow_stock 批量写入失败: {e}")
            return 0

    def upsert_moneyflow_mkt(self, row: dict) -> bool:
        """写入大盘资金流向单行（来自 Tushare moneyflow_mkt_dc / 东财 API）

        字段映射: trade_date, ts_code, net_amount,
                  buy_elg_amount, sell_elg_amount,
                  buy_lg_amount, sell_lg_amount,
                  buy_md_amount, sell_md_amount,
                  buy_sm_amount, sell_sm_amount
        """
        if not self._ensure_conn():
            return False
        if not row or not row.get("trade_date"):
            return False

        try:
            cur = self.conn.cursor()
            sql = """INSERT OR REPLACE INTO moneyflow_mkt
                (trade_date, ts_code, net_amount,
                 buy_elg_amount, sell_elg_amount,
                 buy_lg_amount, sell_lg_amount,
                 buy_md_amount, sell_md_amount,
                 buy_sm_amount, sell_sm_amount)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"""
            cur.execute(sql, (
                str(row["trade_date"]),
                str(row.get("ts_code", "")),
                _safe_float(row.get("net_amount")),
                _safe_float(row.get("buy_elg_amount")),
                _safe_float(row.get("sell_elg_amount")),
                _safe_float(row.get("buy_lg_amount")),
                _safe_float(row.get("sell_lg_amount")),
                _safe_float(row.get("buy_md_amount")),
                _safe_float(row.get("sell_md_amount")),
                _safe_float(row.get("buy_sm_amount")),
                _safe_float(row.get("sell_sm_amount")),
            ))
            self.conn.commit()
            return True
        except Exception as e:
            print(f"[DB] moneyflow_mkt 写入失败: {e}")
            return False

    def upsert_ths_daily(self, df: pd.DataFrame) -> int:
        """写入概念板块每日数据"""
        if not self._ensure_conn():
            return 0
        if df is None or df.empty:
            return 0

        rows = []
        for _, row in df.iterrows():
            r = {
                "ts_code": str(row.get("ts_code", "")),
                "trade_date": str(row.get("trade_date", row.get("data_date", ""))),
                "name": str(row.get("name", "")),
                "pct_chg": _safe_float(row.get("pct_chg")),
                "strength": _safe_float(row.get("strength")),
                "anomaly": 1 if row.get("anomaly") else 0,
            }
            if not r["ts_code"] or not r["trade_date"]:
                continue
            rows.append(r)

        if not rows:
            return 0

        try:
            cur = self.conn.cursor()
            sql = """INSERT OR REPLACE INTO ths_daily
                (ts_code, trade_date, name, pct_chg, strength, anomaly)
                VALUES (?, ?, ?, ?, ?, ?)"""
            cur.executemany(sql, [
                (r["ts_code"], r["trade_date"], r["name"],
                 r["pct_chg"], r["strength"], r["anomaly"])
                for r in rows
            ])
            self.conn.commit()
            print(f"[DB] ths_daily 写入 {len(rows)} 行")
            return len(rows)
        except Exception as e:
            print(f"[DB] ths_daily 写入失败: {e}")
            return 0

    def update_stock_basic(self, df: pd.DataFrame) -> int:
        """更新股票基础信息"""
        if not self._ensure_conn():
            return 0
        if df is None or df.empty:
            return 0

        rows = []
        for _, row in df.iterrows():
            r = {
                "ts_code": str(row.get("ts_code", "")),
                "name": str(row.get("name", "")),
                "market": str(row.get("market", row.get("ts_code", "")[-2:])),
                "industry": str(row.get("industry", "")),
                "list_status": str(row.get("list_status", "L")),
                "list_date": str(row.get("list_date", "")),
                "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
            }
            if not r["ts_code"]:
                continue
            rows.append(r)

        if not rows:
            return 0

        try:
            cur = self.conn.cursor()
            sql = """INSERT OR REPLACE INTO stock_basic
                (ts_code, name, market, industry, list_status, list_date, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)"""
            cur.executemany(sql, [
                (r["ts_code"], r["name"], r["market"], r["industry"],
                 r["list_status"], r["list_date"], r["updated_at"])
                for r in rows
            ])
            self.conn.commit()
            print(f"[DB] stock_basic 写入 {len(rows)} 行")
            return len(rows)
        except Exception as e:
            print(f"[DB] stock_basic 写入失败: {e}")
            return 0

    # ============================================================
    #  fund_basic — ETF基金基础信息
    # ============================================================

    def upsert_fund_basic(self, df: pd.DataFrame) -> int:
        """批量写入ETF基础信息（INSERT OR REPLACE）

        Args:
            df: fund_basic API返回的DataFrame

        Returns:
            写入行数
        """
        if not self._ensure_conn():
            return 0
        if df is None or df.empty:
            return 0

        rows = []
        for _, row in df.iterrows():
            r = {
                "ts_code": str(row.get("ts_code", "")),
                "name": str(row.get("name", "")),
                "fund_type": str(row.get("fund_type", "")),
                "invest_type": str(row.get("invest_type", "")),
                "management": str(row.get("management", "")),
                "custodian": str(row.get("custodian", "")),
                "found_date": str(row.get("found_date", "")),
                "list_date": str(row.get("list_date", "")),
                "delist_date": str(row.get("delist_date", "")),
                "m_fee": _safe_float(row.get("m_fee")),
                "c_fee": _safe_float(row.get("c_fee")),
                "issue_amount": _safe_float(row.get("issue_amount")),
                "benchmark": str(row.get("benchmark", "")),
                "status": str(row.get("status", "L")),
                "market": str(row.get("market", "E")),
                "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
            }
            if not r["ts_code"]:
                continue
            rows.append(r)

        if not rows:
            return 0

        try:
            cur = self.conn.cursor()
            sql = """INSERT OR REPLACE INTO fund_basic
                (ts_code, name, fund_type, invest_type, management, custodian,
                 found_date, list_date, delist_date, m_fee, c_fee,
                 issue_amount, benchmark, status, market, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"""
            cur.executemany(sql, [
                (r["ts_code"], r["name"], r["fund_type"], r["invest_type"],
                 r["management"], r["custodian"], r["found_date"], r["list_date"],
                 r["delist_date"], r["m_fee"], r["c_fee"], r["issue_amount"],
                 r["benchmark"], r["status"], r["market"], r["updated_at"])
                for r in rows
            ])
            self.conn.commit()
            print(f"[DB] fund_basic 写入 {len(rows)} 行")
            return len(rows)
        except Exception as e:
            print(f"[DB] fund_basic 写入失败: {e}")
            return 0

    def get_etf_codes(self, status: str = "L") -> list:
        """获取ETF代码列表

        Args:
            status: L=上市 D=退市 默认L

        Returns:
            ts_code列表
        """
        if not self._ensure_conn():
            return []
        try:
            cur = self.conn.cursor()
            cur.execute("SELECT ts_code FROM fund_basic WHERE status=? ORDER BY ts_code", (status,))
            return [r[0] for r in cur.fetchall()]
        except Exception as e:
            print(f"[DB] get_etf_codes 查询失败: {e}")
            return []

    def get_lagging_etf_codes(self, target_date: str) -> list:
        """查询在ETF日线中缺失某日数据的ETF代码列表

        Args:
            target_date: 目标日期 (YYYYMMDD)

        Returns:
            缺失数据的ETF代码列表
        """
        if not self._ensure_conn():
            return []
        try:
            cur = self.conn.cursor()
            cur.execute("""
                SELECT f.ts_code FROM fund_basic f
                WHERE f.status = 'L'
                AND f.ts_code NOT IN (
                    SELECT DISTINCT ts_code FROM daily_price
                    WHERE trade_date = ? AND asset_type = 'F'
                )
            """, (target_date,))
            return [r[0] for r in cur.fetchall()]
        except Exception as e:
            print(f"[DB] get_lagging_etf_codes 查询失败: {e}")
            return []

    def get_etf_freshness(self, latest_td: str) -> dict:
        """检查ETF日线数据新鲜度

        Args:
            latest_td: 最新交易日 YYYYMMDD

        Returns:
            {"max_date": str, "behind": int, "lagging_count": int}
        """
        if not self._ensure_conn():
            return {"max_date": "", "behind": 999, "lagging_count": 0}
        try:
            cur = self.conn.cursor()
            cur.execute("SELECT MAX(trade_date) FROM daily_price WHERE asset_type='F'")
            row = cur.fetchone()
            max_date = row[0] if row and row[0] else ""

            behind = 0
            if max_date and latest_td > max_date:
                behind = 1  # 简化判断，交易日历逐日查留给auto_sync
            elif not max_date:
                behind = 999

            # 滞后ETF数量
            if max_date and max_date >= latest_td:
                lagging = 0
            else:
                lagging = self.get_lagging_etf_codes(latest_td)
                lagging = len(lagging) if isinstance(lagging, list) else 0

            return {"max_date": max_date, "behind": behind, "lagging_count": lagging}
        except Exception as e:
            print(f"[DB] get_etf_freshness 查询失败: {e}")
            return {"max_date": "", "behind": 999, "lagging_count": 0}

    # ============================================================
    #  index_basic — 指数基础信息
    # ============================================================

    def upsert_index_basic(self, df: pd.DataFrame) -> int:
        """批量写入指数基础信息（INSERT OR REPLACE）

        Args:
            df: index_basic API返回的DataFrame

        Returns:
            写入行数
        """
        if not self._ensure_conn():
            return 0
        if df is None or df.empty:
            return 0

        rows = []
        for _, row in df.iterrows():
            r = {
                "ts_code": str(row.get("ts_code", "")),
                "name": str(row.get("name", "")),
                "market": str(row.get("market", "")),
                "publisher": str(row.get("publisher", "")),
                "category": str(row.get("category", "")),
                "base_date": str(row.get("base_date", "")),
                "base_point": _safe_float(row.get("base_point")),
                "list_date": str(row.get("list_date", "")),
                "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
            }
            if not r["ts_code"]:
                continue
            rows.append(r)

        if not rows:
            return 0

        try:
            cur = self.conn.cursor()
            sql = """INSERT OR REPLACE INTO index_basic
                (ts_code, name, market, publisher, category,
                 base_date, base_point, list_date, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"""
            cur.executemany(sql, [
                (r["ts_code"], r["name"], r["market"], r["publisher"],
                 r["category"], r["base_date"], r["base_point"],
                 r["list_date"], r["updated_at"])
                for r in rows
            ])
            self.conn.commit()
            print(f"[DB] index_basic 写入 {len(rows)} 行")
            return len(rows)
        except Exception as e:
            print(f"[DB] index_basic 写入失败: {e}")
            return 0

    def get_index_codes(self, market: str = None) -> list:
        """获取指数代码列表

        Args:
            market: SSE/SZSE/CSI，None=全部

        Returns:
            ts_code列表
        """
        if not self._ensure_conn():
            return []
        try:
            cur = self.conn.cursor()
            if market:
                cur.execute("SELECT ts_code FROM index_basic WHERE market=? ORDER BY ts_code", (market,))
            else:
                cur.execute("SELECT ts_code FROM index_basic ORDER BY ts_code")
            return [r[0] for r in cur.fetchall()]
        except Exception as e:
            print(f"[DB] get_index_codes 查询失败: {e}")
            return []

    def save_portfolio_snapshot(self, snap_date: str, holdings: list) -> int:
        """
        保存持仓快照

        Args:
            snap_date: 快照日期 YYYY-MM-DD
            holdings: 持仓列表，每项含 ts_code, name, type, cost_price, current_price,
                      shares, market_value, pnl_pct, position_pct
        """
        if not self._ensure_conn():
            return 0
        if not holdings or not snap_date:
            return 0

        rows = []
        for h in holdings:
            code = h.get("代码", h.get("ts_code", ""))
            if not code or code == "000000":
                continue
            rows.append((
                snap_date,
                code,
                h.get("名称", h.get("name", "")),
                h.get("类型", h.get("asset_type", "股票")),
                _safe_float(h.get("成本价", h.get("cost_price"))),
                _safe_float(h.get("当前价", h.get("current_price"))),
                _safe_int(h.get("持股数量", h.get("shares"))),
                _safe_float(h.get("市值", h.get("market_value"))),
                _safe_float(h.get("盈亏比例", h.get("pnl_pct"))),
                _safe_float(h.get("仓位比例", h.get("position_pct"))),
            ))

        if not rows:
            return 0

        try:
            cur = self.conn.cursor()
            sql = """INSERT OR REPLACE INTO portfolio_snapshot
                (snap_date, ts_code, name, asset_type, cost_price, current_price,
                 shares, market_value, pnl_pct, position_pct)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"""
            cur.executemany(sql, rows)
            self.conn.commit()
            print(f"[DB] portfolio_snapshot 写入 {len(rows)} 行 ({snap_date})")
            return len(rows)
        except Exception as e:
            print(f"[DB] portfolio_snapshot 写入失败: {e}")
            return 0

    def save_report_log(self, report_date: str, agent_id: str,
                        status: str = "ok", file_path: str = None,
                        error_msg: str = None) -> bool:
        """记录报告运行日志"""
        if not self._ensure_conn():
            return False

        try:
            cur = self.conn.cursor()
            sql = """INSERT OR REPLACE INTO report_log
                (report_date, agent_id, status, file_path, error_msg)
                VALUES (?, ?, ?, ?, ?)"""
            cur.execute(sql, (report_date, agent_id, status, file_path, error_msg))
            self.conn.commit()
            return True
        except Exception as e:
            print(f"[DB] report_log 写入失败: {e}")
            return False

    # ============================================================
    #  读取方法
    # ============================================================

    def get_daily_price(self, ts_code: str, start_date: str = None,
                        end_date: str = None) -> pd.DataFrame:
        """
        从数据库读取日线行情

        Args:
            ts_code: 股票/指数代码
            start_date: 起始日期 YYYYMMDD（含）
            end_date: 结束日期 YYYYMMDD（含）

        Returns:
            DataFrame，按 trade_date 升序排列
        """
        if not self._ensure_conn():
            return pd.DataFrame()

        try:
            conditions = ["ts_code = ?"]
            params = [ts_code]
            if start_date:
                conditions.append("trade_date >= ?")
                params.append(start_date)
            if end_date:
                conditions.append("trade_date <= ?")
                params.append(end_date)

            sql = f"""SELECT ts_code, trade_date, open, high, low, close,
                       pre_close, change, pct_chg, vol, amount
                FROM daily_price
                WHERE {' AND '.join(conditions)}
                ORDER BY trade_date ASC"""
            return pd.read_sql_query(sql, self.conn, params=params)
        except Exception as e:
            print(f"[DB] get_daily_price 查询失败 ({ts_code}): {e}")
            return pd.DataFrame()

    def get_latest_indicator(self, ts_code: str) -> dict:
        """获取某只股票最新的技术指标"""
        if not self._ensure_conn():
            return {}

        try:
            cur = self.conn.cursor()
            cur.execute(
                """SELECT * FROM daily_indicator
                   WHERE ts_code = ?
                   ORDER BY trade_date DESC LIMIT 1""",
                (ts_code,)
            )
            row = cur.fetchone()
            if row:
                cols = [d[0] for d in cur.description]
                return dict(zip(cols, row))
        except Exception as e:
            print(f"[DB] get_latest_indicator 失败 ({ts_code}): {e}")
        return {}

    def get_indicator_history(self, ts_code: str, start_date: str = None,
                              end_date: str = None) -> pd.DataFrame:
        """获取某只股票的技术指标历史"""
        if not self._ensure_conn():
            return pd.DataFrame()

        try:
            conditions = ["ts_code = ?"]
            params = [ts_code]
            if start_date:
                conditions.append("trade_date >= ?")
                params.append(start_date)
            if end_date:
                conditions.append("trade_date <= ?")
                params.append(end_date)

            sql = f"""SELECT * FROM daily_indicator
                WHERE {' AND '.join(conditions)}
                ORDER BY trade_date ASC"""
            return pd.read_sql_query(sql, self.conn, params=params)
        except Exception as e:
            print(f"[DB] get_indicator_history 失败 ({ts_code}): {e}")
            return pd.DataFrame()

    def get_daily_basic(self, ts_code: str, trade_date: str = None) -> dict:
        """获取某只股票的基本面指标"""
        if not self._ensure_conn():
            return {}

        try:
            cur = self.conn.cursor()
            if trade_date:
                cur.execute(
                    "SELECT * FROM daily_basic WHERE ts_code = ? AND trade_date = ?",
                    (ts_code, trade_date)
                )
            else:
                cur.execute(
                    "SELECT * FROM daily_basic WHERE ts_code = ? ORDER BY trade_date DESC LIMIT 1",
                    (ts_code,)
                )
            row = cur.fetchone()
            if row:
                cols = [d[0] for d in cur.description]
                return dict(zip(cols, row))
        except Exception as e:
            print(f"[DB] get_daily_basic 失败 ({ts_code}): {e}")
        return {}

    def get_daily_basic_from_db(self, ts_code: str, trade_date: str) -> dict:
        """
        直接从 daily_basic 表读取某日估值数据（DB优先，不触发Tushare API）。

        Args:
            ts_code: 股票代码
            trade_date: 交易日 YYYYMMDD

        Returns:
            含 pe/pb/turnover_rate/volume_ratio 等的字典，无数据返回空字典
        """
        if not self._ensure_conn():
            return {}
        try:
            cur = self.conn.cursor()
            cur.execute(
                "SELECT * FROM daily_basic WHERE ts_code = ? AND trade_date = ?",
                (ts_code, trade_date)
            )
            row = cur.fetchone()
            if row:
                cols = [d[0] for d in cur.description]
                return dict(zip(cols, row))
        except Exception as e:
            print(f"[DB] get_daily_basic_from_db 失败 ({ts_code}, {trade_date}): {e}")
        return {}

    def get_sector_ranking(self, trade_date: str, top_n: int = 10) -> pd.DataFrame:
        """获取某日板块涨跌排名"""
        if not self._ensure_conn():
            return pd.DataFrame()

        try:
            cur = self.conn.cursor()
            cur.execute(
                """SELECT * FROM ths_daily
                   WHERE trade_date = ?
                   ORDER BY pct_chg DESC LIMIT ?""",
                (trade_date, top_n)
            )
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description]
            return pd.DataFrame(rows, columns=cols)
        except Exception as e:
            print(f"[DB] get_sector_ranking 失败: {e}")
            return pd.DataFrame()

    def get_moneyflow_stock(self, ts_code: str, trade_date: str) -> dict:
        """获取某只股票在某日的资金流向（从moneyflow_stock表）"""
        if not self._ensure_conn():
            return {}

        try:
            cur = self.conn.cursor()
            cur.execute(
                "SELECT * FROM moneyflow_stock WHERE ts_code = ? AND trade_date = ?",
                (ts_code, trade_date)
            )
            row = cur.fetchone()
            if row:
                cols = [d[0] for d in cur.description]
                return dict(zip(cols, row))
        except Exception as e:
            print(f"[DB] get_moneyflow_stock 失败 ({ts_code}): {e}")
        return {}

    def get_moneyflow_hsgt(self, trade_date: str) -> dict:
        """获取某日北向资金流向"""
        if not self._ensure_conn():
            return {}

        try:
            cur = self.conn.cursor()
            cur.execute(
                "SELECT * FROM moneyflow_hsgt WHERE trade_date = ?",
                (trade_date,)
            )
            row = cur.fetchone()
            if row:
                cols = [d[0] for d in cur.description]
                return dict(zip(cols, row))
        except Exception as e:
            print(f"[DB] get_moneyflow_hsgt 失败: {e}")
        return {}

    # ============================================================
    #  达尔文12.0 新增 — 封装原本散布在Agent脚本中的裸SQL查询
    # ============================================================

    def get_lockup_schedule(self, ts_code: str, start_date: str = "",
                            end_date: str = "") -> list:
        """
        查询股票限售股解禁日程。

        Args:
            ts_code: 股票代码
            start_date: 起始日期 YYYYMMDD
            end_date: 截止日期 YYYYMMDD

        Returns:
            解禁记录列表，每项含 unlock_date/unlock_volume/unlock_ratio/holder_name/lockup_type
        """
        if not self._ensure_conn():
            return []
        try:
            cur = self.conn.cursor()
            sql = """SELECT unlock_date, unlock_volume, unlock_ratio, holder_name, lockup_type
                     FROM lockup_schedule
                     WHERE ts_code = ? AND unlock_date >= ? AND unlock_date <= ?
                     ORDER BY unlock_date ASC"""
            cur.execute(sql, (ts_code, start_date, end_date))
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in rows]
        except Exception as e:
            print(f"[DB] get_lockup_schedule 失败 ({ts_code}): {e}")
            return []

    def get_margin_detail(self, ts_code: str, start_date: str = "",
                          end_date: str = "") -> list:
        """
        查询个股融资融券明细。

        Args:
            ts_code: 股票代码
            start_date: 起始日期 YYYYMMDD
            end_date: 截止日期 YYYYMMDD

        Returns:
            融资融券记录列表（按日期降序）
        """
        if not self._ensure_conn():
            return []
        try:
            cur = self.conn.cursor()
            sql = """SELECT trade_date, rzye, rqye, rzmre, rqmcl, rzrqye
                     FROM margin_detail
                     WHERE ts_code = ? AND trade_date >= ? AND trade_date <= ?
                     ORDER BY trade_date DESC"""
            cur.execute(sql, (ts_code, start_date, end_date))
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in rows]
        except Exception as e:
            print(f"[DB] get_margin_detail 失败 ({ts_code}): {e}")
            return []

    def get_dragon_tiger_detail(self, ts_code: str, start_date: str = "") -> list:
        """
        查询龙虎榜明细。

        Args:
            ts_code: 股票代码
            start_date: 起始日期 YYYYMMDD

        Returns:
            龙虎榜记录列表（按日期降序）
        """
        if not self._ensure_conn():
            return []
        try:
            cur = self.conn.cursor()
            sql = """SELECT trade_date, close, pct_chg, amount, buy_amount, sell_amount,
                            net_amount, buy_seats, sell_seats, reason_type
                     FROM dragon_tiger_detail
                     WHERE ts_code = ? AND trade_date >= ?
                     ORDER BY trade_date DESC"""
            cur.execute(sql, (ts_code, start_date))
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in rows]
        except Exception as e:
            print(f"[DB] get_dragon_tiger_detail 失败 ({ts_code}): {e}")
            return []

    def get_policy_events(self, since_date: str = "", days: int = 30) -> list:
        """
        查询政策事件。

        Args:
            since_date: 起始日期 YYYYMMDD（为空时取 days 天前）
            days: 回溯天数（since_date 为空时生效）

        Returns:
            政策事件列表
        """
        if not self._ensure_conn():
            return []
        try:
            import datetime as dt
            if not since_date:
                since_date = (dt.datetime.now() - dt.timedelta(days=days)).strftime("%Y%m%d")
            cur = self.conn.cursor()
            cur.execute(
                "SELECT title, event_date, category FROM policy_events WHERE event_date >= ? ORDER BY event_date DESC",
                (since_date,)
            )
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in rows]
        except Exception as e:
            print(f"[DB] get_policy_events 失败: {e}")
            return []

    def upsert_policy_event(self, data: dict) -> bool:
        """
        写入一条政策事件。

        Args:
            data: 含 event_date/title/content/source/category/impact_sector/impact_score/url

        Returns:
            是否写入成功
        """
        if not self._ensure_conn():
            return False
        try:
            cur = self.conn.cursor()
            cur.execute(
                """INSERT OR IGNORE INTO policy_events
                   (event_date, title, content, source, category, impact_sector, impact_score, url)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (data.get("event_date"), data.get("title"), data.get("content"),
                 data.get("source"), data.get("category"), data.get("impact_sector"),
                 data.get("impact_score"), data.get("url"))
            )
            self.conn.commit()
            return cur.rowcount > 0
        except Exception as e:
            print(f"[DB] upsert_policy_event 失败: {e}")
            return False

    def upsert_dragon_tiger_detail(self, data: dict) -> bool:
        """
        写入一条龙虎榜明细。

        Args:
            data: 含 trade_date/ts_code/name/close/pct_chg/amount/
                  buy_amount/sell_amount/net_amount/buy_seats/sell_seats/reason_type

        Returns:
            是否写入成功
        """
        if not self._ensure_conn():
            return False
        try:
            cur = self.conn.cursor()
            cur.execute(
                """INSERT OR IGNORE INTO dragon_tiger_detail
                   (trade_date, ts_code, name, close, pct_chg, amount,
                    buy_amount, sell_amount, net_amount, buy_seats, sell_seats, reason_type)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (data.get("trade_date"), data.get("ts_code"), data.get("name"),
                 data.get("close"), data.get("pct_chg"), data.get("amount"),
                 data.get("buy_amount"), data.get("sell_amount"), data.get("net_amount"),
                 json.dumps(data.get("buy_seats", []), ensure_ascii=False) if isinstance(data.get("buy_seats"), list) else data.get("buy_seats"),
                 json.dumps(data.get("sell_seats", []), ensure_ascii=False) if isinstance(data.get("sell_seats"), list) else data.get("sell_seats"),
                 data.get("reason_type"))
            )
            self.conn.commit()
            return cur.rowcount > 0
        except Exception as e:
            print(f"[DB] upsert_dragon_tiger_detail 失败: {e}")
            return False

    def upsert_hot_money_seats(self, data: dict) -> bool:
        """
        写入一条游资席位记录。

        Args:
            data: 含 trade_date/seat_name/seat_type/style/active_stocks

        Returns:
            是否写入成功
        """
        if not self._ensure_conn():
            return False
        try:
            cur = self.conn.cursor()
            cur.execute(
                """INSERT OR IGNORE INTO hot_money_seats
                   (trade_date, seat_name, seat_type, style, active_stocks)
                   VALUES (?, ?, ?, ?, ?)""",
                (data.get("trade_date"), data.get("seat_name"), data.get("seat_type"),
                 data.get("style"), data.get("active_stocks"))
            )
            self.conn.commit()
            return cur.rowcount > 0
        except Exception as e:
            print(f"[DB] upsert_hot_money_seats 失败: {e}")
            return False

    def get_latest_trade_date(self) -> str:
        """获取数据库中最新交易日"""
        if not self._ensure_conn():
            return ""

        try:
            cur = self.conn.cursor()
            cur.execute("SELECT MAX(trade_date) FROM daily_price")
            row = cur.fetchone()
            return row[0] if row and row[0] else ""
        except Exception as e:
            print(f"  [WARN] get_latest_trade_date 失败: {e}")
            return ""

    def has_data_for_date(self, ts_code: str, trade_date: str) -> bool:
        """检查数据库中是否已有某日某股的数据"""
        if not self._ensure_conn():
            return False

        try:
            cur = self.conn.cursor()
            cur.execute(
                "SELECT 1 FROM daily_price WHERE ts_code = ? AND trade_date = ?",
                (ts_code, trade_date)
            )
            return cur.fetchone() is not None
        except Exception as e:
            print(f"  [WARN] has_data_for_date({ts_code}, {trade_date}) 失败: {e}")
            return False

    def get_table_freshness(self) -> dict:
        """
        一次性获取所有表的数据新鲜度。

        Returns:
            {
                "daily_price": {"max_date": "20260701", "stock_count_at_max": 3731, "total_stocks": 3731},
                "daily_basic": {"max_date": "20260701", "stock_count_at_max": 5500},
                "adj_factor": {"max_date": "20260701", "stock_count": 3731},
                "fina_indicator": {"max_date": "20260331", "stock_count": 3731},
                "dividend": {"max_date": "20260630", "stock_count": 3757},
            }
        """
        if not self._ensure_conn():
            return {}

        result = {}
        try:
            cur = self.conn.cursor()

            # daily_price
            cur.execute("SELECT MAX(trade_date) FROM daily_price")
            row = cur.fetchone()
            dp_max = row[0] if row and row[0] else ""
            dp_count = 0
            if dp_max:
                cur.execute(
                    "SELECT COUNT(DISTINCT ts_code) FROM daily_price WHERE trade_date = ?",
                    (dp_max,)
                )
                dp_count = cur.fetchone()[0]
            cur.execute("SELECT COUNT(DISTINCT ts_code) FROM daily_price")
            dp_total = cur.fetchone()[0]
            result["daily_price"] = {
                "max_date": dp_max, "stock_count_at_max": dp_count, "total_stocks": dp_total
            }

            # daily_basic
            cur.execute("SELECT MAX(trade_date), COUNT(DISTINCT ts_code) FROM daily_basic")
            row = cur.fetchone()
            result["daily_basic"] = {
                "max_date": row[0] if row and row[0] else "",
                "total_stocks": row[1] if row and row[1] else 0,
            }

            # adj_factor
            cur.execute("SELECT MAX(trade_date), COUNT(DISTINCT ts_code) FROM adj_factor")
            row = cur.fetchone()
            result["adj_factor"] = {
                "max_date": row[0] if row and row[0] else "",
                "total_stocks": row[1] if row and row[1] else 0,
            }

            # fina_indicator (uses end_date, not trade_date)
            cur.execute("SELECT MAX(end_date), COUNT(DISTINCT ts_code) FROM fina_indicator")
            row = cur.fetchone()
            result["fina_indicator"] = {
                "max_date": row[0] if row and row[0] else "",
                "total_stocks": row[1] if row and row[1] else 0,
            }

            # dividend (uses end_date)
            cur.execute("SELECT MAX(end_date), COUNT(DISTINCT ts_code) FROM dividend")
            row = cur.fetchone()
            result["dividend"] = {
                "max_date": row[0] if row and row[0] else "",
                "total_stocks": row[1] if row and row[1] else 0,
            }

        except Exception as e:
            print(f"[DB] get_table_freshness 失败: {e}")
        return result

    def get_lagging_stocks(self, table: str, target_date: str,
                           asset_type: str = 'E') -> list:
        """
        查询在指定表中缺失某日数据的股票代码列表。

        Args:
            table: 表名 (daily_price, adj_factor, fina_indicator, dividend)
            target_date: 目标日期 (YYYYMMDD)
            asset_type: 资产类型过滤 (仅 daily_price 使用)

        Returns:
            缺失数据的股票代码列表
        """
        if not self._ensure_conn():
            return []

        try:
            cur = self.conn.cursor()

            if table == "daily_price":
                # 查询有 stock_basic 记录但在 daily_price 中缺少 target_date 的股票
                cur.execute("""
                    SELECT s.ts_code FROM stock_basic s
                    WHERE s.list_status = 'L'
                    AND s.ts_code NOT IN (
                        SELECT DISTINCT ts_code FROM daily_price
                        WHERE trade_date = ?
                    )
                """, (target_date,))
            elif table == "adj_factor":
                cur.execute("""
                    SELECT s.ts_code FROM stock_basic s
                    WHERE s.list_status = 'L'
                    AND NOT EXISTS (
                        SELECT 1 FROM adj_factor a
                        WHERE a.ts_code = s.ts_code AND a.trade_date = ?
                    )
                """, (target_date,))
            elif table == "fina_indicator":
                # 财报用 end_date，检查最新季度
                cur.execute("""
                    SELECT s.ts_code FROM stock_basic s
                    WHERE s.list_status = 'L'
                    AND s.ts_code NOT IN (
                        SELECT DISTINCT ts_code FROM fina_indicator
                        WHERE end_date = ?
                    )
                """, (target_date,))
            elif table == "dividend":
                # 分红数据不需要按日期逐条检查，检查是否有 end_date >= 上一个除权日
                cur.execute("""
                    SELECT s.ts_code FROM stock_basic s
                    WHERE s.list_status = 'L'
                    AND NOT EXISTS (
                        SELECT 1 FROM dividend d
                        WHERE d.ts_code = s.ts_code AND d.end_date >= ?
                    )
                """, (target_date,))
            else:
                return []

            return [row[0] for row in cur.fetchall()]
        except Exception as e:
            print(f"[DB] get_lagging_stocks({table}, {target_date}) 失败: {e}")
            return []

    def get_portfolio_snapshot(self, snap_date: str = None) -> pd.DataFrame:
        """获取持仓快照（默认最新）"""
        if not self._ensure_conn():
            return pd.DataFrame()

        try:
            if snap_date:
                sql = "SELECT * FROM portfolio_snapshot WHERE snap_date = ?"
                params = [snap_date]
            else:
                sql = """SELECT * FROM portfolio_snapshot
                         WHERE snap_date = (SELECT MAX(snap_date) FROM portfolio_snapshot)"""
                params = []
            return pd.read_sql_query(sql, self.conn, params=params)
        except Exception as e:
            print(f"[DB] get_portfolio_snapshot 失败: {e}")
            return pd.DataFrame()

    def get_report_status(self, report_date: str) -> pd.DataFrame:
        """查看某日各 Agent 报告生成状态"""
        if not self._ensure_conn():
            return pd.DataFrame()

        try:
            return pd.read_sql_query(
                "SELECT * FROM report_log WHERE report_date = ?",
                self.conn, params=[report_date]
            )
        except Exception as e:
            print(f"[DB] get_report_status 失败: {e}")
            return pd.DataFrame()

    # ============================================================
    #  决策审计日志（TradingAgents借鉴）
    # ============================================================

    def log_decision(self, log_date: str, decision_type: str, action: str,
                     summary: str = "", reasoning: str = "",
                     risk_level: str = "LOW",
                     conflict_count: int = 0, rework_count: int = 0,
                     sources: str = "") -> bool:
        """写入一条决策审计日志

        TradingAgents 使用 SQLite 持久化每笔交易决策的全链条推理过程，
        实现完全白盒的审计跟踪。我们将其扩展为记录投资领导每次最终决策。

        Args:
            log_date: 决策日期 YYYYMMDD
            decision_type: 决策类型 (trade/hold/risk_adjust/rework)
            action: 最终行动描述
            summary: 决策摘要
            reasoning: 推理过程简述
            risk_level: 风险等级 (LOW/MEDIUM/HIGH)
            conflict_count: 冲突项数量
            rework_count: 打回重做项数量
            sources: 决策依据来源
        Returns:
            bool: 是否成功
        """
        if not self._ensure_conn():
            return False
        try:
            self.conn.execute("""
                INSERT INTO decision_log
                    (log_date, decision_type, action, summary, reasoning,
                     risk_level, conflict_count, rework_count, sources)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (log_date, decision_type, action, summary, reasoning,
                  risk_level, conflict_count, rework_count, sources))
            self.conn.commit()
            return True
        except Exception as e:
            print(f"[DB] log_decision 失败: {e}")
            return False

    def get_decision_history(self, days: int = 30) -> pd.DataFrame:
        """获取最近N天决策历史"""
        if not self._ensure_conn():
            return pd.DataFrame()
        try:
            return pd.read_sql_query(
                """SELECT * FROM decision_log
                   WHERE log_date >= date('now', '-' || ? || ' days', 'localtime')
                   ORDER BY created_at DESC""",
                self.conn, params=[str(days)]
            )
        except Exception as e:
            print(f"[DB] get_decision_history 失败: {e}")
            return pd.DataFrame()

    # ============================================================
    #  统计与维护方法
    # ============================================================

    def get_table_stats(self) -> dict:
        """获取各表数据量统计"""
        if not self._ensure_conn():
            return {}

        tables = [
            "stock_basic", "daily_price", "daily_basic", "fina_indicator",
            "dividend", "adj_factor", "daily_indicator", "moneyflow_hsgt",
            "ths_daily", "moneyflow_mkt", "margin",
            "portfolio_snapshot", "watchlist", "report_log", "decision_log",
            "fund_basic", "index_basic", "policy_events",
            "dragon_tiger_detail", "hot_money_seats", "lockup_schedule",
            "margin_detail", "moneyflow_stock",
        ]
        stats = {}
        try:
            cur = self.conn.cursor()
            for table in tables:
                cur.execute(f"SELECT COUNT(*) FROM {table}")
                count = cur.fetchone()[0]
                stats[table] = count
        except Exception as e:
            print(f"[DB] get_table_stats 失败: {e}")
        return stats

    def get_date_range(self, table: str = "daily_price") -> tuple:
        """获取某表的数据日期范围 (最早, 最晚)"""
        if not self._ensure_conn():
            return ("", "")

        try:
            cur = self.conn.cursor()
            cur.execute(f"SELECT MIN(trade_date), MAX(trade_date) FROM {table}")
            row = cur.fetchone()
            return (row[0] or "", row[1] or "")
        except Exception as e:
            print(f"  [WARN] get_date_range({table}) 失败: {e}")
            return ("", "")

    def vacuum(self):
        """清理数据库碎片（建议定期执行）"""
        if not self._ensure_conn():
            return
        try:
            self.conn.execute("VACUUM")
            print("[DB] VACUUM 完成")
        except Exception as e:
            print(f"[DB] VACUUM 失败: {e}")


# ============================================================
#  共享工具函数
# ============================================================

def get_daily_price_db_first(ts_code: str, days_back: int = 60, caller: str = "") -> pd.DataFrame:
    """
    获取日线行情：DB优先→Tushare API→DataProvider(Tushare→AkShare自动fallback)。

    原本在 stock_picker.py / risk_overlay.py / scorecard.py 三处重复定义，
    统一提取到此作为通用工具函数。三层fallback确保最大数据可用性。

    Args:
        ts_code: 股票代码（如 000001.SZ）
        days_back: 取最近多少天的数据
        caller: 调用方标识（用于日志标签，如 "risk_overlay"）

    Returns:
        DataFrame，按 trade_date 降序（最新在前），数据不足时返回空DataFrame
    """
    tag = f"[{caller}]" if caller else "[db_manager]"
    try:
        _db = DatabaseManager()
        df = _db.get_daily_price(ts_code)
        if df is not None and not df.empty and len(df) >= 10:
            df = df.sort_values("trade_date", ascending=False).reset_index(drop=True)
            return df.head(days_back)
    except Exception as e:
        print(f"  [WARN] {tag} DB行情读取失败 ({ts_code}): {e}")

    # Fallback to Tushare API
    try:
        from scripts.utils.tushare_client import pro
        df = pro.daily(ts_code=ts_code)
        if df is not None and not df.empty:
            df = df.sort_values("trade_date", ascending=False).reset_index(drop=True)
            return df.head(days_back)
    except Exception as e:
        print(f"  [WARN] {tag} Tushare行情失败 ({ts_code}): {e}")

    # 最终兜底：DataProvider（Tushare→AkShare自动fallback）
    try:
        from scripts.utils.data_provider import get_provider
        dp = get_provider()
        df = dp.daily(ts_code=ts_code)
        if df is not None and not df.empty:
            df = df.sort_values("trade_date", ascending=False).reset_index(drop=True)
            return df.head(days_back)
    except Exception as e:
        print(f"  [WARN] {tag} DataProvider行情失败 ({ts_code}): {e}")
    return pd.DataFrame()


# ============================================================
#  自检
# ============================================================

if __name__ == "__main__":
    print("=" * 60)
    print("  SQLite 股票数据库管理器 — 自检")
    print("=" * 60)

    db = DatabaseManager()

    # 1. 建表检查
    print("\n[1] 建表检查...")
    stats = db.get_table_stats()
    for table, count in stats.items():
        print(f"    {table}: {count} 行")

    # 2. 写入测试
    print("\n[2] 写入测试...")
    test_df = pd.DataFrame([{
        "ts_code": "000001.SZ",
        "trade_date": "20260701",
        "open": 12.5, "high": 12.8, "low": 12.3, "close": 12.6,
        "pre_close": 12.4, "change": 0.2, "pct_chg": 1.61,
        "vol": 500000, "amount": 6300000,
    }])
    n = db.upsert_daily_price(test_df, asset_type='E')
    print(f"    写入 daily_price: {n} 行")

    test_indicator = pd.DataFrame([{
        "ts_code": "000001.SZ",
        "trade_date": "20260701",
        "ma_5": 12.5, "ma_10": 12.3, "ma_20": 12.0, "ma_60": 11.5,
        "macd": 0.15, "macd_signal": 0.10, "macd_diff": 0.05,
        "kdj_k": 60.0, "kdj_d": 55.0, "kdj_j": 70.0,
        "rsi_14": 58.0,
        "boll_upper": 13.0, "boll_mid": 12.0, "boll_lower": 11.0, "boll_width": 0.17,
    }])
    n2 = db.upsert_daily_indicator(test_indicator)
    print(f"    写入 daily_indicator: {n2} 行")

    # 3. 读取测试
    print("\n[3] 读取测试...")
    df = db.get_daily_price("000001.SZ", "20260101", "20260701")
    print(f"    daily_price 查询: {len(df)} 行")
    if not df.empty:
        print(f"    最新: {df.iloc[-1]['ts_code']} {df.iloc[-1]['trade_date']} close={df.iloc[-1]['close']}")

    ind = db.get_latest_indicator("000001.SZ")
    print(f"    最新指标: MACD={ind.get('macd')}, RSI={ind.get('rsi_14')}, MA5={ind.get('ma_5')}")

    latest = db.get_latest_trade_date()
    print(f"    最新交易日: {latest}")

    # 4. 清理测试数据
    print("\n[4] 清理测试数据...")
    try:
        db.conn.execute("DELETE FROM daily_price WHERE ts_code='000001.SZ' AND trade_date='20260701'")
        db.conn.execute("DELETE FROM daily_indicator WHERE ts_code='000001.SZ' AND trade_date='20260701'")
        db.conn.commit()
        print("    测试数据已清理")
    except Exception as e:
        print(f"   [WARN] 测试数据清理失败: {e}")

    # 5. 数据范围
    print("\n[5] 数据范围...")
    date_range = db.get_date_range()
    print(f"    daily_price 范围: {date_range[0]} ~ {date_range[1]}")

    db.close()
    print("\n[DB] 自检完成")
