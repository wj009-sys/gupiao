"""
自动数据同步脚本 — 检查数据新鲜度并按需拉取缺失数据

用法:
    # 快速检查模式（SessionStart hook 使用，<1秒）
    python scripts/utils/auto_sync.py --check-only

    # 自动同步模式（Cron 18:00 使用）
    python scripts/utils/auto_sync.py --auto-sync

    # 只同步特定类型
    python scripts/utils/auto_sync.py --auto-sync --type daily_price

    # 限制时间和数量
    python scripts/utils/auto_sync.py --auto-sync --max-minutes 30 --max-stocks 500

数据同步优先级（按速度排序）:
  0. fund_basic     — ETF元数据                       ⚡ 仅首次
  1. index_basic    — 指数元数据                       ⚡ 仅首次
  2. daily_basic    — pro.daily_basic(trade_date=X)    ⚡ 1次/天
  3. index_daily    — pro.index_daily(ts_code=X)       ⚡ ~3.5min/天
  4. etf_daily      — pro.fund_daily(ts_code=X)        🐢 ~9min/天
  5. daily_price    — pro.daily(ts_code=X) 逐股拉缺失  🐢 ~22min/天
  6. adj_factor     — pro.adj_factor(ts_code=X)        中等
  7. fina_indicator — pro.fina_indicator(ts_code=X)    仅季度缺口
  8. dividend       — pro.dividend(ts_code=X)          很少需要
  9. ths_daily      — pro.ths_daily(trade_date=X)      快
  10. moneyflow_hsgt — pro.moneyflow_hsgt() 北向资金    ⚡ 1次/天
  11. moneyflow_stock — pro.moneyflow(ts_code=X) 持仓  慢（仅限持仓+自选）
  12. quality_check — 覆盖度检查+多源回补+数据清洗     快速
  13. daily_indicator — 全市场技术指标计算              快速

D3异常处理表:
| 触发条件 | 一线修复 | 仍失败兜底 |
|---------|---------|-----------|
| Tushare API超时/限频 | 等待3秒重试1次 | 跳过该股票/日期，汇总报告 |
| API返回空数据（非交易日）| 正常，标记为"已同步" | 继续处理 |
| 数据库连接失败 | 重新连接1次 | 终止脚本，输出错误 |
| 单类型超过max_minutes | 中断当前类型 | 标记"部分完成"，继续下个类型 |
| 今日是周末/节假日 | 直接输出状态，不拉取 | 这是正常行为，exit 0 |
| 交易日历缓存失效 | 重新从Tushare拉取 | 如果Tushare也失败，用星期一~五估算 |
| stock_basic 表为空 | 先拉取stock_basic | 如果也失败，降级为从daily_price取DISTINCT ts_code |

D4 CHECKPOINT:
- CP1-日期验证：拉取前确认 trade_date > db_max_date，防止倒退覆盖
- CP2-行数验证：拉取后行数增量必须 >= 0（不允许数据减少）
- CP3-覆盖率验证：daily_basic 拉取后确认当日覆盖股票数 >= 2000
- CP4-汇总一致性：最终报告的落后天数与实际拉取的行数逻辑一致

D9反例：
- 不要在非交易日拉取 daily 数据（API会返回空，浪费配额）
- 不要在循环中无间隔调用API（daily_price≥0.2s, daily_basic≥0.12s, 财报≥0.3s）
- 不要假设所有股票都有所有类型数据（ETF无财报、债券无估值）
- 不要用 print 输出进度时没有 flush（后台任务输出会被缓冲）
- 不要在检查模式下调 Tushare API（--check-only 必须纯 SQL）
"""

import os
import sys
import json
import time
import argparse
from datetime import datetime, timedelta

import pandas as pd
import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from scripts.utils.tushare_client import pro
from scripts.utils.db_manager import DatabaseManager

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
CALENDAR_CACHE = os.path.join(PROJECT_ROOT, "data", "trading_calendar.json")

# 数据优先级注释（按速度排序）:
#   S+  daily_basic     — pro.daily_basic(trade_date=X) 按日期批量  ⚡ 1次/天
#   S+  fund_basic      — pro.fund_basic(market='E') ETF元数据      ⚡ 1次（首次）
#   S+  index_basic     — pro.index_basic(market=X) 指数元数据      ⚡ 1次（首次）
#   S   index_daily     — pro.index_daily(ts_code=X) 全部指数       ⚡ ~3.5 min/天
#   A   etf_daily       — pro.fund_daily(ts_code=X) ETF日线         🐢 ~9 min/天
#   A   daily_price     — pro.daily(ts_code=X) 逐股拉缺失日         🐢 ~22 min/天
#   B   adj_factor      — pro.adj_factor(ts_code=X)                 中等
#   C   fina_indicator  — pro.fina_indicator(ts_code=X)             仅季度缺口
#   C   dividend        — pro.dividend(ts_code=X)                   很少需要

# 非股票前缀（ETF/债券/老三板）
NON_STOCK_PREFIXES = ("51", "159", "588", "11", "71", "73", "40")


def _now() -> str:
    """当前时间字符串"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def get_trading_calendar(force_refresh: bool = False) -> list:
    """
    获取交易日历（本地JSON缓存 + Tushare增量更新）

    首次运行时从 Tushare 拉取全部交易日并缓存到 data/trading_calendar.json。
    后续运行如果缓存的最新日期 < 今天，增量拉取补充。

    Returns:
        排序后的交易日列表 (YYYYMMDD)
    """
    today = datetime.now().strftime("%Y%m%d")

    # 读取缓存
    cached_dates = []
    if not force_refresh and os.path.exists(CALENDAR_CACHE):
        try:
            with open(CALENDAR_CACHE, "r", encoding="utf-8") as f:
                data = json.load(f)
                cached_dates = data.get("dates", [])
        except (json.JSONDecodeError, FileNotFoundError, PermissionError, OSError, AttributeError, TypeError) as e:
            print(f"  [WARN] 交易日历缓存读取失败: {e}，将重新拉取", flush=True)
            cached_dates = []

    # 如果缓存足够新（最后日期 >= 今天），直接返回
    if cached_dates and cached_dates[-1] >= today:
        return cached_dates

    # 从 Tushare 拉取
    try:
        pull_start = "20200101"
        if cached_dates:
            # 增量：从缓存最后日期+1开始
            last_cached = datetime.strptime(cached_dates[-1], "%Y%m%d")
            pull_start = (last_cached + timedelta(days=1)).strftime("%Y%m%d")

        print(f"  [交易日历] 从Tushare拉取 {pull_start}~{today}...", flush=True)
        df = pro.trade_cal(exchange="SSE", start_date=pull_start, end_date=today)
        if df is not None and not df.empty:
            is_open = df[df["is_open"] == 1]
            new_dates = sorted(is_open["cal_date"].tolist())

            if cached_dates:
                # 合并（去重）
                all_dates = sorted(set(cached_dates + new_dates))
            else:
                all_dates = new_dates

            # 保存缓存
            os.makedirs(os.path.dirname(CALENDAR_CACHE), exist_ok=True)
            with open(CALENDAR_CACHE, "w", encoding="utf-8") as f:
                json.dump({
                    "source": "tushare trade_cal",
                    "exchange": "SSE",
                    "last_updated": today,
                    "count": len(all_dates),
                    "dates": all_dates,
                }, f, ensure_ascii=False)
            print(f"  [交易日历] 共 {len(all_dates)} 个交易日，已缓存", flush=True)
            return all_dates
    except Exception as e:
        print(f"  [WARN] 交易日历拉取失败: {e}，使用缓存+估算", flush=True)

    # 如果拉取失败但有缓存，用缓存
    if cached_dates:
        return cached_dates

    # 什么都没有：用简单的周一~五估算（不准确但不会阻塞）
    print("  [WARN] 无交易日历缓存，使用周一~五估算（不准确）", flush=True)
    dates = []
    d = datetime(2020, 1, 1)
    end = datetime.now()
    while d <= end:
        if d.weekday() < 5:  # Mon-Fri
            dates.append(d.strftime("%Y%m%d"))
        d += timedelta(days=1)
    return dates


def get_latest_trading_day(trading_days: list) -> str:
    """获取最新交易日（<= 今天）"""
    today = datetime.now().strftime("%Y%m%d")
    valid = [d for d in trading_days if d <= today]
    return valid[-1] if valid else today


def is_trading_day(trading_days: list) -> bool:
    """判断今天是否为交易日"""
    today = datetime.now().strftime("%Y%m%d")
    return today in trading_days


def check_freshness(db: DatabaseManager, trading_days: list) -> dict:
    """
    SQL-only 数据新鲜度检查（< 1 秒，不调 Tushare API）

    Returns:
        {
            "check_time": "2026-07-02 12:00:00",
            "today": "20260702",
            "is_trading_day": True,
            "latest_trading_day": "20260702",
            "tables": { ... },
            "overall_status": "fresh" | "stale" | "behind",
            "summary": "一句话总结"
        }
    """
    today = datetime.now().strftime("%Y%m%d")
    latest_td = get_latest_trading_day(trading_days)
    is_td = today in trading_days

    result = {
        "check_time": _now(),
        "today": today,
        "is_trading_day": is_td,
        "latest_trading_day": latest_td,
        "tables": {},
        "overall_status": "fresh",
        "summary": "",
    }

    freshness = db.get_table_freshness()
    if not freshness:
        result["overall_status"] = "error"
        result["summary"] = "无法连接数据库"
        return result

    # === daily_price ===
    dp = freshness.get("daily_price", {})
    dp_max = dp.get("max_date", "")
    dp_behind = 0
    if dp_max and latest_td:
        dp_behind = sum(1 for d in trading_days if dp_max < d <= latest_td)
    dp_lagging = 0
    if dp_max and latest_td and dp_max < latest_td:
        try:
            cur = db.conn.cursor()
            cur.execute("""
                SELECT COUNT(*) FROM stock_basic WHERE list_status = 'L'
                AND ts_code NOT IN (
                    SELECT DISTINCT ts_code FROM daily_price WHERE trade_date = ?
                )
            """, (latest_td,))
            dp_lagging = cur.fetchone()[0]
        except Exception as e:
            print(f"  [WARN] lagging_stocks查询失败: {e}", flush=True)
            dp_lagging = 0

    dp_status = "fresh"
    if dp_behind == 0:
        dp_status = "fresh"
    elif dp_behind <= 2:
        dp_status = "stale"
    else:
        dp_status = "behind"

    result["tables"]["daily_price"] = {
        "max_date": dp_max,
        "behind_trading_days": dp_behind,
        "lagging_stocks": dp_lagging,
        "total_stocks": dp.get("total_stocks", 0),
        "status": dp_status,
    }

    # === daily_basic ===
    dbasic = freshness.get("daily_basic", {})
    dbasic_max = dbasic.get("max_date", "")
    dbasic_behind = 0
    if dbasic_max and latest_td:
        dbasic_behind = sum(1 for d in trading_days if dbasic_max < d <= latest_td)
    dbasic_status = "fresh" if dbasic_behind == 0 else ("stale" if dbasic_behind <= 2 else "behind")
    result["tables"]["daily_basic"] = {
        "max_date": dbasic_max,
        "behind_trading_days": dbasic_behind,
        "status": dbasic_status,
    }

    # === adj_factor ===
    adj = freshness.get("adj_factor", {})
    adj_max = adj.get("max_date", "")
    adj_behind = 0
    if adj_max and latest_td:
        adj_behind = sum(1 for d in trading_days if adj_max < d <= latest_td)
    adj_status = "fresh" if adj_behind == 0 else ("stale" if adj_behind <= 2 else "behind")
    result["tables"]["adj_factor"] = {
        "max_date": adj_max,
        "behind_trading_days": adj_behind,
        "status": adj_status,
    }

    # === fina_indicator (quarterly, uses end_date) ===
    fina = freshness.get("fina_indicator", {})
    fina_max = fina.get("max_date", "")
    fina_status = "fresh"
    # 财报滞后是正常的（季报在季度结束后1-2个月内发布）
    if fina_max:
        try:
            max_dt = datetime.strptime(fina_max, "%Y%m%d")
            months_behind = (datetime.now() - max_dt).days / 30
            if months_behind > 5:
                fina_status = "stale"
        except Exception as e:
            print(f"  [WARN] fina_indicator日期解析失败: {e}", flush=True)

    result["tables"]["fina_indicator"] = {
        "max_date": fina_max,
        "status": fina_status,
    }

    # === ETF日线 (asset_type='F') ===
    try:
        etf_fresh = db.get_etf_freshness(latest_td)
        etf_max = etf_fresh.get("max_date", "")
        etf_behind = 0
        if etf_max and latest_td:
            etf_behind = sum(1 for d in trading_days if etf_max < d <= latest_td)
        elif not etf_max:
            etf_behind = 999  # 空表
        etf_status = "fresh" if etf_behind == 0 else ("stale" if etf_behind <= 2 else "behind")
    except Exception as e:
        etf_status = "unknown"
        etf_max = ""
        etf_behind = 0
    result["tables"]["etf_daily"] = {
        "max_date": etf_max,
        "behind_trading_days": etf_behind,
        "status": etf_status,
    }

    # === dividend ===
    div = freshness.get("dividend", {})
    div_max = div.get("max_date", "")
    div_status = "fresh"
    if div_max:
        try:
            max_dt = datetime.strptime(div_max, "%Y%m%d")
            days_behind = (datetime.now() - max_dt).days
            if days_behind > 30:
                div_status = "stale"
        except Exception as e:
            print(f"  [WARN] dividend日期解析失败: {e}", flush=True)

    result["tables"]["dividend"] = {
        "max_date": div_max,
        "status": div_status,
    }

    # === 汇总判断 ===
    any_stale = any(
        t.get("status") in ("stale", "behind")
        for t in result["tables"].values()
    )
    if any_stale:
        result["overall_status"] = "stale"
        stale_names = [
            name for name, t in result["tables"].items()
            if t.get("status") in ("stale", "behind")
        ]
        result["summary"] = f"数据落后 — {', '.join(stale_names)} 需要更新"
    else:
        result["overall_status"] = "fresh"
        result["summary"] = "所有数据已是最新"

    return result


def print_freshness_report(freshness: dict):
    """格式化输出新鲜度报告（stdout → Claude 上下文）"""
    print(f"\n## 📊 数据新鲜度检查 — {freshness['check_time']}")
    print(f"今天: {freshness['today']} "
          f"({'交易日' if freshness['is_trading_day'] else '非交易日'}) "
          f"| 最新交易日: {freshness['latest_trading_day']}")
    print()

    # 表头
    print(f"| {'表':<18} | {'最新日期':>10} | {'落后(交易日)':>12} | {'滞后股票':>10} | {'状态':>6} |")
    print(f"| {'-'*18} | {'-'*10} | {'-'*12} | {'-'*10} | {'-'*6} |")

    status_icons = {"fresh": "✅", "stale": "⚠️", "behind": "🔴", "empty": "⛔"}

    for table_name, info in freshness["tables"].items():
        icon = status_icons.get(info.get("status", ""), "❓")
        behind = info.get("behind_trading_days", "-")
        if isinstance(behind, int) and behind > 0:
            behind = f"+{behind}"
        lagging = info.get("lagging_stocks", "-")
        if isinstance(lagging, int) and lagging > 0:
            lagging_str = f"{lagging}"
        elif lagging == 0 or lagging == "-":
            lagging_str = "-"
        else:
            lagging_str = str(lagging)

        print(f"| {table_name:<18} | {info.get('max_date', 'N/A'):>10} | "
              f"{str(behind):>12} | {lagging_str:>10} | {icon}{info.get('status', '?'):>5} |")

    print()
    status_label = {"fresh": "✅ FRESH — 数据已是最新",
                    "stale": "⚠️ STALE — 有数据落后，建议运行 --auto-sync",
                    "behind": "🔴 BEHIND — 严重落后，请运行 --auto-sync",
                    "error": "❌ ERROR — 数据库连接失败"}
    print(f"**{status_label.get(freshness['overall_status'], freshness['summary'])}**")
    print()

    # 如果非交易日
    if not freshness["is_trading_day"]:
        print("> ℹ️ 今天不是交易日，无需拉取数据。")
        print()

    # 建议命令
    if freshness["overall_status"] != "fresh":
        print("> 运行 `python scripts/utils/auto_sync.py --auto-sync` 补齐数据")


# ============================================================
#  同步函数
# ============================================================

def sync_daily_basic(db: DatabaseManager, missing_dates: list,
                     time_budget_sec: float) -> dict:
    """
    同步 daily_basic（按日期批量拉取，速度最快）

    Args:
        db: 数据库管理器
        missing_dates: 缺失的交易日列表
        time_budget_sec: 时间预算（秒）

    Returns:
        {"pulled_dates": N, "rows_inserted": N, "errors": N, "elapsed_sec": F}
    """
    result = {"pulled_dates": 0, "rows_inserted": 0, "errors": 0, "elapsed_sec": 0}
    if not missing_dates:
        return result

    print(f"\n{'='*60}")
    print(f"  [2/10] 每日估值 (daily_basic) — {len(missing_dates)} 天")
    print(f"{'='*60}")

    start_time = time.time()

    for i, trade_date in enumerate(missing_dates):
        if time.time() - start_time > time_budget_sec:
            print(f"  ⏰ 时间预算用完，已处理 {i}/{len(missing_dates)} 天", flush=True)
            break

        try:
            df = pro.daily_basic(
                trade_date=trade_date,
                fields="ts_code,trade_date,pe,pe_ttm,pb,total_mv,circ_mv,turnover_rate,volume_ratio"
            )
            if df is not None and not df.empty:
                n = db.upsert_daily_basic(df)
                result["rows_inserted"] += n
                result["pulled_dates"] += 1
                print(f"  [{i+1}/{len(missing_dates)}] {trade_date} → {n}只股票", flush=True)
            else:
                print(f"  [{i+1}/{len(missing_dates)}] {trade_date} → 无数据（可能非交易日）", flush=True)
        except Exception as e:
            result["errors"] += 1
            print(f"  [{i+1}/{len(missing_dates)}] {trade_date} → 失败: {e}", flush=True)

        if i < len(missing_dates) - 1:
            time.sleep(0.12)

    result["elapsed_sec"] = time.time() - start_time
    print(f"  daily_basic 完成: {result['pulled_dates']}天 {result['rows_inserted']:,}行 "
          f"({result['elapsed_sec']:.1f}s)", flush=True)
    return result


def sync_fund_basic(db: DatabaseManager) -> dict:
    """
    拉取全部上市ETF元数据（仅首次执行，后续跳过）

    Args:
        db: 数据库管理器

    Returns:
        {"total": N, "elapsed_sec": F}
    """
    result = {"total": 0, "elapsed_sec": 0}

    # 检查是否已有数据
    existing = db.get_etf_codes()
    if existing:
        print(f"\n  fund_basic — ETF元数据已存在 ({len(existing)}只)，跳过")
        return result

    print(f"\n{'='*60}")
    print(f"  ETF元数据 (fund_basic) — 首次全量拉取")
    print(f"{'='*60}")

    start_time = time.time()
    try:
        df = pro.fund_basic(market='E', status='L')
        if df is not None and not df.empty:
            n = db.upsert_fund_basic(df)
            result["total"] = n
            print(f"  ETF元数据写入: {n}只", flush=True)
        else:
            print(f"  fund_basic 返回空数据", flush=True)
    except Exception as e:
        print(f"  fund_basic 拉取失败: {e}", flush=True)

    result["elapsed_sec"] = time.time() - start_time
    print(f"  ETF元数据完成: {result['total']}只 ({result['elapsed_sec']:.1f}s)", flush=True)
    return result


def sync_index_basic(db: DatabaseManager) -> dict:
    """
    拉取全部指数元数据（仅首次执行，后续跳过）

    覆盖: SSE(208) + SZSE(485) + CSI(精选~300)
    注: CSI共有8000+指数，只保留宽基/行业/策略/风格等有用类别

    Args:
        db: 数据库管理器

    Returns:
        {"total": N, "elapsed_sec": F}
    """
    result = {"total": 0, "elapsed_sec": 0}

    # 检查是否已有数据
    existing = db.get_index_codes()
    if existing:
        print(f"\n  index_basic — 指数元数据已存在 ({len(existing)}只)，跳过")
        return result

    print(f"\n{'='*60}")
    print(f"  指数元数据 (index_basic) — 首次全量拉取")
    print(f"{'='*60}")

    start_time = time.time()
    total = 0

    # SSE 上证指数(208只)
    try:
        df = pro.index_basic(market='SSE')
        if df is not None and not df.empty:
            total += db.upsert_index_basic(df)
            print(f"  SSE指数: {len(df)}只", flush=True)
    except Exception as e:
        print(f"  SSE index_basic 失败: {e}", flush=True)
    time.sleep(0.3)

    # SZSE 深证指数(485只)
    try:
        df = pro.index_basic(market='SZSE')
        if df is not None and not df.empty:
            total += db.upsert_index_basic(df)
            print(f"  SZSE指数: {len(df)}只", flush=True)
    except Exception as e:
        print(f"  SZSE index_basic 失败: {e}", flush=True)
    time.sleep(0.3)

    # CSI 中证指数(8000+只 — 只保留有用的)
    try:
        df = pro.index_basic(market='CSI')
        if df is not None and not df.empty:

            def _is_base_csi_code(code):
                """标准CSI代码: 6位数字+.CSI，排除多货币变体"""
                parts = code.split('.')
                return len(parts) == 2 and len(parts[0]) == 6 and parts[0].isdigit()

            def _is_useful_index(row):
                code = str(row.get("ts_code", ""))
                name = str(row.get("name", ""))
                # 只保留标准格式代码
                if not _is_base_csi_code(code):
                    return False
                # 000前缀: 宽基指数(121只) 全部保留
                if code.startswith("000"):
                    return True
                # 930前缀: 行业/策略指数(352只) 全部保留
                if code.startswith("930"):
                    return True
                # 931/932前缀: 按关键词精选
                if code.startswith("931") or code.startswith("932"):
                    useful_kw = ["策略", "风格", "主题", "成长", "价值",
                                "红利", "低波", "龙头", "ESG", "质量", "动量",
                                "消费", "医药", "科技", "金融", "制造",
                                "能源", "材料", "公用", "行业"]
                    if any(kw in name for kw in useful_kw):
                        return True
                return False

            useful = df[df.apply(_is_useful_index, axis=1)]
            if not useful.empty:
                total += db.upsert_index_basic(useful)
                print(f"  CSI指数: 原始{len(df)}只 → 筛选后{len(useful)}只有用", flush=True)
            else:
                print(f"  CSI指数: 原始{len(df)}只，筛选后无可用指数", flush=True)
    except Exception as e:
        print(f"  CSI index_basic 失败: {e}", flush=True)

    result["total"] = total
    result["elapsed_sec"] = time.time() - start_time
    print(f"  指数元数据完成: {result['total']}只 ({result['elapsed_sec']:.1f}s)", flush=True)
    return result


def sync_index_daily(db: DatabaseManager, start_date: str, end_date: str,
                      time_budget_sec: float = 600) -> dict:
    """
    同步全部已注册指数日线

    Args:
        db: 数据库管理器
        start_date: 起始日期
        end_date: 截止日期
        time_budget_sec: 时间预算（秒）

    Returns:
        {"indices": N, "rows_inserted": N, "errors": N, "elapsed_sec": F}
    """
    result = {"indices": 0, "rows_inserted": 0, "errors": 0, "skipped_timeout": 0, "elapsed_sec": 0}

    # 从 index_basic 获取全部指数代码
    index_codes = db.get_index_codes()
    if not index_codes:
        print(f"\n  index_daily — 指数列表为空，跳过（请先运行 index_basic 同步）")
        return result

    print(f"\n{'='*60}")
    print(f"  [3/10] 指数日线 (index_daily) — {len(index_codes)} 个指数")
    print(f"  日期范围: {start_date} ~ {end_date}")
    print(f"{'='*60}")

    start_time = time.time()

    for i, ts_code in enumerate(index_codes):
        # 时间预算检查
        if time.time() - start_time > time_budget_sec:
            result["skipped_timeout"] = len(index_codes) - i
            print(f"  ⏰ 时间预算用完 ({time.time() - start_time:.0f}s)，剩余 {result['skipped_timeout']} 个跳过", flush=True)
            break

        try:
            df = pro.index_daily(ts_code=ts_code, start_date=start_date, end_date=end_date)
            if df is not None and not df.empty:
                df = df.sort_values("trade_date").reset_index(drop=True)
                if "vol" in df.columns:
                    df = df.rename(columns={"vol": "volume"})
                n = db.upsert_daily_price(df, asset_type='I')
                result["rows_inserted"] += n
                result["indices"] += 1
                if (i + 1) % 50 == 0 or i == len(index_codes) - 1:
                    elapsed = time.time() - start_time
                    eta = elapsed / (i + 1) * (len(index_codes) - i - 1)
                    print(f"  [{i+1}/{len(index_codes)}] 完成{result['indices']}个 | "
                          f"⏱{elapsed/60:.1f}m ETA{eta/60:.1f}m", flush=True)
            else:
                result["errors"] += 1
                if result["errors"] <= 3:
                    print(f"  [{i+1}/{len(index_codes)}] {ts_code} → 无数据", flush=True)
        except Exception as e:
            result["errors"] += 1
            if result["errors"] <= 3:
                print(f"  [{i+1}/{len(index_codes)}] {ts_code} → 失败: {e}", flush=True)

        if i < len(index_codes) - 1:
            time.sleep(0.2)

    result["elapsed_sec"] = time.time() - start_time
    print(f"  指数日线完成: {result['indices']}个 {result['rows_inserted']:,}行 "
          f"({result['elapsed_sec']/60:.1f}分钟)", flush=True)
    return result


def sync_etf_daily(db: DatabaseManager, start_date: str, end_date: str,
                    time_budget_sec: float) -> dict:
    """
    增量同步ETF日线行情（逐只拉取）

    Args:
        db: 数据库管理器
        start_date: 起始日期
        end_date: 截止日期
        time_budget_sec: 时间预算（秒）

    Returns:
        {"pulled_etfs": N, "rows_inserted": N, "errors": N, "elapsed_sec": F}
    """
    result = {"pulled_etfs": 0, "rows_inserted": 0,
              "errors": 0, "skipped_timeout": 0, "elapsed_sec": 0}

    etf_codes = db.get_etf_codes()
    if not etf_codes:
        print(f"\n  etf_daily — ETF列表为空，跳过（请先运行 fund_basic 同步）")
        return result

    print(f"\n{'='*60}")
    print(f"  [4/10] ETF日线 (etf_daily) — {len(etf_codes)} 只")
    print(f"  日期范围: {start_date} ~ {end_date}")
    print(f"{'='*60}")

    start_time = time.time()

    for i, ts_code in enumerate(etf_codes):
        # 时间预算检查
        elapsed = time.time() - start_time
        if elapsed > time_budget_sec:
            result["skipped_timeout"] = len(etf_codes) - i
            print(f"  ⏰ 时间预算用完 ({elapsed:.0f}s)，剩余 {result['skipped_timeout']} 只跳过", flush=True)
            break

        try:
            df = pro.fund_daily(ts_code=ts_code, start_date=start_date, end_date=end_date)
            if df is not None and not df.empty:
                # fund_daily 已有 vol 列（无需重命名）
                n = db.upsert_daily_price(df, asset_type='F')
                result["rows_inserted"] += n
                result["pulled_etfs"] += 1
            # 空数据也算已处理
            result["pulled_etfs"] += 1 if df is None or df.empty else 0
        except Exception as e:
            result["errors"] += 1
            if result["errors"] <= 5:
                print(f"  [{i+1}/{len(etf_codes)}] {ts_code} → 失败: {e}", flush=True)

        # 进度输出（每200只）
        if (i + 1) % 200 == 0 or i == len(etf_codes) - 1:
            elapsed = time.time() - start_time
            eta = elapsed / (i + 1) * (len(etf_codes) - i - 1) if (i + 1) > 0 else 0
            print(f"  [{i+1}/{len(etf_codes)}] "
                  f"成功{result['pulled_etfs']} 失败{result['errors']} "
                  f"| ⏱{elapsed/60:.1f}m ETA{eta/60:.1f}m", flush=True)

        if i < len(etf_codes) - 1:
            time.sleep(0.25)  # ETFs限频

    result["elapsed_sec"] = time.time() - start_time
    print(f"  ETF日线完成: {result['pulled_etfs']}只 {result['rows_inserted']:,}行 "
          f"({result['elapsed_sec']/60:.1f}分钟)", flush=True)
    return result


def sync_daily_price_incremental(db: DatabaseManager, lagging_stocks: list,
                                  start_date: str, end_date: str,
                                  time_budget_sec: float,
                                  max_stocks: int = 0) -> dict:
    """
    增量同步 daily_price（逐股拉取缺失日期）

    优化：按 last_date 升序排列，最落后的股票优先处理

    Args:
        db: 数据库管理器
        lagging_stocks: 缺失数据的股票列表
        start_date: 拉取起始日期
        end_date: 拉取截止日期
        time_budget_sec: 时间预算（秒）
        max_stocks: 最多拉取股票数（0=全部）

    Returns:
        {"pulled_stocks": N, "rows_inserted": N, "errors": N, "elapsed_sec": F}
    """
    result = {"pulled_stocks": 0, "rows_inserted": 0,
              "errors": 0, "skipped_timeout": 0, "elapsed_sec": 0}

    if not lagging_stocks:
        return result

    # 限制数量
    stocks_to_pull = lagging_stocks[:max_stocks] if max_stocks > 0 else lagging_stocks

    print(f"\n{'='*60}")
    print(f"  [5/10] 日线行情 (daily_price) — {len(stocks_to_pull)} 只滞后股票")
    print(f"  日期范围: {start_date} ~ {end_date}")
    print(f"{'='*60}")

    start_time = time.time()
    last_progress = 0
    PROGRESS_INTERVAL = 100  # 每100只输出一次进度

    for i, code in enumerate(stocks_to_pull):
        # 时间预算检查
        elapsed = time.time() - start_time
        if elapsed > time_budget_sec:
            result["skipped_timeout"] = len(stocks_to_pull) - i
            print(f"  ⏰ 时间预算用完 ({elapsed:.0f}s)，剩余 {result['skipped_timeout']} 只跳过", flush=True)
            break

        try:
            df = pro.daily(ts_code=code, start_date=start_date, end_date=end_date)
            if df is not None and not df.empty:
                n = db.upsert_daily_price(df, asset_type='E')
                result["rows_inserted"] += n
                result["pulled_stocks"] += 1
            # 空数据 = 可能退市或停牌，也算"已处理"
            result["pulled_stocks"] += 1 if df is None or df.empty else 0
        except Exception as e:
            result["errors"] += 1
            if result["errors"] <= 5:
                print(f"  [{i+1}/{len(stocks_to_pull)}] {code} → 失败: {e}", flush=True)

        # 进度输出
        if (i + 1) - last_progress >= PROGRESS_INTERVAL:
            elapsed = time.time() - start_time
            eta = elapsed / (i + 1) * len(stocks_to_pull) - elapsed
            print(f"  [{i+1}/{len(stocks_to_pull)}] "
                  f"成功{result['pulled_stocks']} 失败{result['errors']} "
                  f"| ⏱{elapsed/60:.1f}m ETA{eta/60:.1f}m", flush=True)
            last_progress = i + 1

        if i < len(stocks_to_pull) - 1:
            time.sleep(0.25)  # daily_price 限频

    result["elapsed_sec"] = time.time() - start_time
    print(f"  daily_price 完成: {result['pulled_stocks']}只 {result['rows_inserted']:,}行 "
          f"({result['elapsed_sec']/60:.1f}分钟)", flush=True)
    return result


def sync_adj_factor_incremental(db: DatabaseManager, all_codes: list,
                                 time_budget_sec: float) -> dict:
    """
    增量同步复权因子（逐股拉取）

    只拉取非ETF/债券的普通股票。adj_factor API 无日期过滤，
    拉取全量历史但 INSERT OR REPLACE 去重。
    """
    result = {"pulled_stocks": 0, "rows_inserted": 0, "errors": 0, "elapsed_sec": 0}

    # 只对股票拉取复权因子
    stock_codes = [c for c in all_codes
                   if not c.startswith(NON_STOCK_PREFIXES)]

    # 检查哪些已存在
    db_existing = set()
    try:
        cur = db.conn.cursor()
        cur.execute("SELECT DISTINCT ts_code FROM adj_factor")
        db_existing = set(row[0] for row in cur.fetchall())
    except Exception as e:
        print(f"  [WARN] adj_factor已存在查询失败: {e}", flush=True)
        db_existing = set()

    stocks_to_pull = [c for c in stock_codes if c not in db_existing]
    if not stocks_to_pull:
        print(f"\n  [6/10] 复权因子 — 所有股票已有数据，跳过")
        return result

    print(f"\n{'='*60}")
    print(f"  [6/10] 复权因子 (adj_factor) — {len(stocks_to_pull)} 只新股票")
    print(f"{'='*60}")

    start_time = time.time()

    for i, code in enumerate(stocks_to_pull):
        if time.time() - start_time > time_budget_sec:
            print(f"  ⏰ 时间预算用完", flush=True)
            break

        try:
            df = pro.adj_factor(ts_code=code)
            if df is not None and not df.empty:
                n = db.upsert_adj_factor(df)
                result["rows_inserted"] += n
                result["pulled_stocks"] += 1
        except Exception as e:
            result["errors"] += 1

        if (i + 1) % 200 == 0:
            print(f"  [{i+1}/{len(stocks_to_pull)}] {code} → {result['pulled_stocks']}成功", flush=True)

        if i < len(stocks_to_pull) - 1:
            time.sleep(0.3)

    result["elapsed_sec"] = time.time() - start_time
    print(f"  复权因子完成: {result['pulled_stocks']}只 {result['rows_inserted']:,}行 "
          f"({result['elapsed_sec']:.1f}s)", flush=True)
    return result


def sync_fina_indicator_incremental(db: DatabaseManager, all_codes: list,
                                     time_budget_sec: float) -> dict:
    """
    增量同步财报数据（逐股拉取，仅处理缺失最新季度的股票）
    """
    result = {"pulled_stocks": 0, "rows_inserted": 0, "errors": 0, "elapsed_sec": 0}

    # 只对股票拉取财报
    stock_codes = [c for c in all_codes
                   if not c.startswith(NON_STOCK_PREFIXES)]

    # 找出数据库中最新财报日期
    try:
        cur = db.conn.cursor()
        cur.execute("SELECT MAX(end_date) FROM fina_indicator")
        max_end_date = cur.fetchone()[0] or ""
    except Exception as e:
        print(f"  [WARN] 财报日期查询失败: {e}")
        max_end_date = ""

    # 找出 end_date < max_end_date 的股票（有新财报未拉取）
    # 对于日常同步，检查是否有股票的最新财报日期不是最新的
    lagging_stocks = []
    if max_end_date:
        try:
            cur = db.conn.cursor()
            cur.execute("""
                SELECT s.ts_code FROM stock_basic s
                WHERE s.list_status = 'L'
                AND s.ts_code NOT LIKE '51%'
                AND s.ts_code NOT LIKE '159%'
                AND s.ts_code NOT LIKE '588%'
                AND s.ts_code NOT IN (
                    SELECT DISTINCT ts_code FROM fina_indicator WHERE end_date = ?
                )
            """, (max_end_date,))
            lagging_stocks = [row[0] for row in cur.fetchall()]
        except Exception as e:
            print(f"  [WARN] 财报滞后查询失败: {e}")
            lagging_stocks = []

    if not lagging_stocks:
        print(f"\n  [7/10] 财报数据 — 所有股票已是最新，跳过")
        return result

    print(f"\n{'='*60}")
    print(f"  [7/10] 财报数据 (fina_indicator) — {len(lagging_stocks)} 只滞后股票")
    print(f"{'='*60}")

    start_time = time.time()
    today = datetime.now().strftime("%Y%m%d")

    for i, code in enumerate(lagging_stocks):
        if time.time() - start_time > time_budget_sec:
            break

        try:
            df = pro.fina_indicator(
                ts_code=code,
                start_date="20200101",
                end_date=today,
                fields="ts_code,end_date,op_income,profit_dedt,roe,roa,"
                       "grossprofit_margin,debt_to_assets,current_ratio,"
                       "or_yoy,dt_netprofit_yoy"
            )
            if df is not None and not df.empty:
                n = db.upsert_fina_indicator(df)
                result["rows_inserted"] += n
                result["pulled_stocks"] += 1
        except Exception as e:
            result["errors"] += 1

        if (i + 1) % 200 == 0:
            print(f"  [{i+1}/{len(lagging_stocks)}] → {result['pulled_stocks']}成功", flush=True)

        if i < len(lagging_stocks) - 1:
            time.sleep(0.35)

    result["elapsed_sec"] = time.time() - start_time
    print(f"  财报数据完成: {result['pulled_stocks']}只 {result['rows_inserted']:,}行 "
          f"({result['elapsed_sec']:.1f}s)", flush=True)
    return result


def sync_dividend_incremental(db: DatabaseManager, all_codes: list,
                               time_budget_sec: float) -> dict:
    """
    增量同步分红送转数据（逐股拉取，仅处理没有分红记录的股票）
    """
    result = {"pulled_stocks": 0, "rows_inserted": 0, "errors": 0, "elapsed_sec": 0}

    # 只对股票拉取
    stock_codes = [c for c in all_codes
                   if not c.startswith(NON_STOCK_PREFIXES)]

    # 找完全没有分红记录的股票
    db_existing = set()
    try:
        cur = db.conn.cursor()
        cur.execute("SELECT DISTINCT ts_code FROM dividend")
        db_existing = set(row[0] for row in cur.fetchall())
    except Exception as e:
        print(f"  [WARN] 分红数据查询失败: {e}")
        db_existing = set()

    stocks_to_pull = [c for c in stock_codes if c not in db_existing]
    if not stocks_to_pull:
        print(f"\n  [8/10] 分红数据 — 所有股票已有数据，跳过")
        return result

    print(f"\n{'='*60}")
    print(f"  [8/10] 分红送转 (dividend) — {len(stocks_to_pull)} 只新股票")
    print(f"{'='*60}")

    start_time = time.time()

    for i, code in enumerate(stocks_to_pull):
        if time.time() - start_time > time_budget_sec:
            break

        try:
            df = pro.dividend(ts_code=code)
            if df is not None and not df.empty:
                n = db.upsert_dividend(df)
                result["rows_inserted"] += n
                result["pulled_stocks"] += 1
        except Exception as e:
            result["errors"] += 1

        if (i + 1) % 500 == 0:
            print(f"  [{i+1}/{len(stocks_to_pull)}] → {result['pulled_stocks']}成功", flush=True)

        if i < len(stocks_to_pull) - 1:
            time.sleep(0.35)

    result["elapsed_sec"] = time.time() - start_time
    print(f"  分红送转完成: {result['pulled_stocks']}只 {result['rows_inserted']:,}行 "
          f"({result['elapsed_sec']:.1f}s)", flush=True)
    return result


def load_all_market_codes(db: DatabaseManager) -> list:
    """从 stock_basic 表加载全市场股票代码"""
    try:
        cur = db.conn.cursor()
        cur.execute("SELECT ts_code FROM stock_basic WHERE list_status = 'L' ORDER BY ts_code")
        codes = [row[0] for row in cur.fetchall()]
        if codes:
            return codes
    except Exception as e:
        print(f"  [WARN] stock_basic 查询失败: {e}")

    # 如果 stock_basic 为空，从 daily_price 获取
    try:
        cur = db.conn.cursor()
        cur.execute("SELECT DISTINCT ts_code FROM daily_price ORDER BY ts_code")
        return [row[0] for row in cur.fetchall()]
    except Exception as e:
        print(f"  [ERROR] 无法加载股票列表: {e}")
        return []


# ============================================================
#  数据质量检查（Post-Sync）
# ============================================================

def post_sync_data_check(db: DatabaseManager, trading_days: list,
                          latest_td: str) -> dict:
    """
    同步后数据质量检查 — 覆盖度 + 多源回补 + 数据清洗（Phase 12）

    三阶段:
      A. 覆盖度检查 — stock/ETF/index 数量与预期对比，标记缺失
      B. 多源回补 — 对缺失数据通过 DataProvider(Tushare→mootdx→Akshare) 回拉
      C. 数据清洗 — 去重 + NULL填充 + 异常值标记

    Args:
        db: 数据库管理器
        trading_days: 交易日列表
        latest_td: 最新交易日

    Returns:
        {"coverage": {...}, "fallback": {...}, "cleaning": {...},
         "errors": N, "elapsed_sec": F}
    """
    result = {
        "coverage": {},
        "fallback": {"pulled": 0, "rows": 0, "errors": 0},
        "cleaning": {"duplicates_removed": 0, "nulls_filled": 0, "anomalies": 0},
        "errors": 0,
        "elapsed_sec": 0,
    }

    print(f"\n{'='*60}")
    print(f"  [10/10] 数据质量检查 (Post-Sync Quality Check)")
    print(f"{'='*60}", flush=True)

    start_time = time.time()

    # ================================================================
    # Phase A: 覆盖度检查
    # ================================================================
    print(f"\n  --- Phase A: 覆盖度检查 ---", flush=True)

    coverage = {}

    # A1. 股票覆盖
    try:
        cur = db.conn.cursor()
        cur.execute("SELECT COUNT(*) FROM stock_basic WHERE list_status='L'")
        expected_stocks = cur.fetchone()[0]
        cur.execute("SELECT COUNT(DISTINCT ts_code) FROM daily_price "
                    "WHERE asset_type='E' AND trade_date=?", (latest_td,))
        actual_stocks = cur.fetchone()[0]
        stocks_ok = actual_stocks >= expected_stocks * 0.95  # 允许5%差异
        coverage["stocks"] = {
            "expected": expected_stocks, "actual": actual_stocks,
            "coverage_pct": round(100.0 * actual_stocks / expected_stocks, 1) if expected_stocks else 0,
            "ok": stocks_ok,
        }
        status = "✅" if stocks_ok else "⚠️"
        print(f"  {status} 股票: {actual_stocks}/{expected_stocks} "
              f"({coverage['stocks']['coverage_pct']}%)", flush=True)
        if not stocks_ok:
            print(f"     → 缺失 {expected_stocks - actual_stocks} 只", flush=True)
    except Exception as e:
        coverage["stocks"] = {"error": str(e), "ok": False}
        result["errors"] += 1

    # A2. ETF覆盖
    try:
        cur.execute("SELECT COUNT(*) FROM fund_basic WHERE status='L'")
        expected_etfs = cur.fetchone()[0]
        if expected_etfs > 0:
            cur.execute("SELECT COUNT(DISTINCT ts_code) FROM daily_price "
                        "WHERE asset_type='F' AND trade_date=?", (latest_td,))
            actual_etfs = cur.fetchone()[0]
            etfs_ok = actual_etfs >= expected_etfs * 0.90  # ETF允许10%差异(流动性差)
            coverage["etfs"] = {
                "expected": expected_etfs, "actual": actual_etfs,
                "coverage_pct": round(100.0 * actual_etfs / expected_etfs, 1) if expected_etfs else 0,
                "ok": etfs_ok,
            }
            status = "✅" if etfs_ok else "⚠️"
            print(f"  {status} ETF: {actual_etfs}/{expected_etfs} "
                  f"({coverage['etfs']['coverage_pct']}%)", flush=True)
        else:
            coverage["etfs"] = {"expected": 0, "actual": 0, "coverage_pct": 0, "ok": True}
    except Exception as e:
        coverage["etfs"] = {"error": str(e), "ok": False}
        result["errors"] += 1

    # A3. 指数覆盖
    try:
        cur.execute("SELECT COUNT(*) FROM index_basic")
        expected_indices = cur.fetchone()[0]
        if expected_indices > 0:
            cur.execute("SELECT COUNT(DISTINCT ts_code) FROM daily_price "
                        "WHERE asset_type='I' AND trade_date=?", (latest_td,))
            actual_indices = cur.fetchone()[0]
            indices_ok = actual_indices >= expected_indices * 0.85  # 指数允许15%差异(低频指数)
            coverage["indices"] = {
                "expected": expected_indices, "actual": actual_indices,
                "coverage_pct": round(100.0 * actual_indices / expected_indices, 1) if expected_indices else 0,
                "ok": indices_ok,
            }
            status = "✅" if indices_ok else "⚠️"
            print(f"  {status} 指数: {actual_indices}/{expected_indices} "
                  f"({coverage['indices']['coverage_pct']}%)", flush=True)
        else:
            coverage["indices"] = {"expected": 0, "actual": 0, "coverage_pct": 0, "ok": True}
    except Exception as e:
        coverage["indices"] = {"error": str(e), "ok": False}
        result["errors"] += 1

    result["coverage"] = coverage

    # ================================================================
    # Phase B: 多源回补 — 对缺失数据尝试其他数据源
    # ================================================================
    fallback_pulled = 0
    fallback_rows = 0
    fallback_errors = 0

    for asset_type, label in [('E', '股票'), ('F', 'ETF'), ('I', '指数')]:
        cov_info = coverage.get({'E': 'stocks', 'F': 'etfs', 'I': 'indices'}[asset_type], {})
        if cov_info.get("ok", True):
            continue  # 覆盖率OK，跳过回补

        missing_pct = 100.0 - cov_info.get("coverage_pct", 100)
        if missing_pct < 5:
            continue  # 缺失比例太小，不浪费API配额

        print(f"\n  --- Phase B: 回补{label}缺失数据 ({missing_pct:.0f}%缺失) ---",
              flush=True)

        source_table = {'E': 'stock_basic', 'F': 'fund_basic', 'I': 'index_basic'}[asset_type]
        status_col = {"E": "list_status='L'", "F": "status='L'", "I": "1=1"}[asset_type]
        try:
            # 只查询当前asset_type对应的源表（不混合UNION全部类型）
            cur.execute(f"""
                SELECT ts_code FROM {source_table}
                WHERE {status_col}
                AND ts_code NOT IN (
                    SELECT DISTINCT ts_code FROM daily_price
                    WHERE asset_type=? AND trade_date=?
                )
            """, (asset_type, latest_td))
            missing_codes = [r[0] for r in cur.fetchall()]
        except Exception as e:
            print(f"    查询缺失{label}失败: {e}", flush=True)
            missing_codes = []

        missing_codes = missing_codes[:50]  # 最多回补50只（防止无限循环）

        if not missing_codes:
            print(f"    无缺失{label}需要回补", flush=True)
            continue

        print(f"    需回补: {len(missing_codes)}只", flush=True)
        for code in missing_codes:
            try:
                # 通过 DataProvider 尝试多源回补
                from scripts.utils.data_provider import get_provider
                provider = get_provider()

                if asset_type == 'F':
                    # ETF: fund_daily 不在 Provider 中，走 DataProvider.daily()
                    # 但DataProvider没有fund_daily，直接用Tushare
                    import tushare as ts
                    from scripts.utils.tushare_client import pro as tushare_pro
                    df = tushare_pro.fund_daily(ts_code=code,
                                                 start_date=latest_td,
                                                 end_date=latest_td)
                    if df is not None and not df.empty:
                        n = db.upsert_daily_price(df, asset_type='F')
                        fallback_rows += n
                        fallback_pulled += 1
                    continue

                if asset_type == 'I':
                    df = provider.index_daily(code, latest_td, latest_td)
                else:
                    df = provider.daily(code, latest_td, latest_td)
                if df is not None and not df.empty:
                    n = db.upsert_daily_price(df, asset_type=asset_type)
                    fallback_rows += n
                    fallback_pulled += 1
            except Exception as e:
                fallback_errors += 1
                if fallback_errors <= 3:
                    print(f"    {code} 回补失败: {e}", flush=True)

            if len(missing_codes) > 1:
                import time as _time
                _time.sleep(0.3)

    result["fallback"] = {
        "pulled": fallback_pulled,
        "rows": fallback_rows,
        "errors": fallback_errors,
    }

    if fallback_pulled > 0:
        print(f"  ✅ 多源回补: {fallback_pulled}只成功 / {fallback_rows}行", flush=True)
    else:
        print(f"  ✅ 多源回补: 无需补拉", flush=True)

    # ================================================================
    # Phase C: 数据清洗 — 去重 + NULL清理
    # ================================================================
    print(f"\n  --- Phase C: 数据清洗 ---", flush=True)

    cleaning = {"duplicates_removed": 0, "nulls_filled": 0, "anomalies": 0}

    # C1. 去重
    try:
        for table in ["daily_price", "daily_basic", "adj_factor"]:
            # 查找重复行
            cur.execute(f"""
                SELECT COUNT(*) FROM (
                    SELECT ts_code, trade_date FROM {table}
                    GROUP BY ts_code, trade_date HAVING COUNT(*) > 1
                )
            """)
            dup_groups = cur.fetchone()[0]
            if dup_groups > 0:
                # 删除重复（保留最小 rowid）
                cur.execute(f"""
                    DELETE FROM {table} WHERE rowid NOT IN (
                        SELECT MIN(rowid) FROM {table}
                        GROUP BY ts_code, trade_date
                    )
                """)
                removed = cur.rowcount
                cleaning["duplicates_removed"] += removed
                if removed > 0:
                    print(f"    {table}: 删除 {removed} 行重复数据", flush=True)
        db.conn.commit()
    except Exception as e:
        print(f"    去重失败: {e}", flush=True)
        result["errors"] += 1

    # C2. NULL填充（可推导字段）
    try:
        # 填充可计算的 pct_chg：pct_chg = (close - pre_close) / pre_close * 100
        cur.execute(f"""
            UPDATE daily_price
            SET pct_chg = ROUND((close - pre_close) / pre_close * 100, 2)
            WHERE trade_date = ? AND pct_chg IS NULL
            AND pre_close IS NOT NULL AND pre_close > 0 AND close IS NOT NULL
        """, (latest_td,))
        filled_pct = cur.rowcount
        if filled_pct > 0:
            cleaning["nulls_filled"] += filled_pct
            print(f"    daily_price: 填充 {filled_pct} 行 NULL pct_chg", flush=True)

        # 填充 NULL vol → 0（无成交量的行）
        cur.execute(f"""
            UPDATE daily_price SET vol = 0
            WHERE trade_date = ? AND vol IS NULL
        """, (latest_td,))
        filled_vol = cur.rowcount
        if filled_vol > 0:
            cleaning["nulls_filled"] += filled_vol
            print(f"    daily_price: 填充 {filled_vol} 行 NULL vol → 0", flush=True)

        # 填充 NULL amount → 0
        cur.execute(f"""
            UPDATE daily_price SET amount = 0
            WHERE trade_date = ? AND amount IS NULL
        """, (latest_td,))
        filled_amt = cur.rowcount
        if filled_amt > 0:
            cleaning["nulls_filled"] += filled_amt
            print(f"    daily_price: 填充 {filled_amt} 行 NULL amount → 0", flush=True)

        db.conn.commit()
    except Exception as e:
        print(f"    NULL填充失败: {e}", flush=True)

    # C3. 异常值检测（price=0 但 vol>0）
    try:
        for atype, alabel in [('E', '股票'), ('F', 'ETF'), ('I', '指数')]:
            cur.execute(f"""
                SELECT COUNT(*) FROM daily_price
                WHERE asset_type=? AND trade_date=?
                AND (close IS NULL OR close <= 0)
                AND vol > 0
            """, (atype, latest_td))
            bad_rows = cur.fetchone()[0]
            if bad_rows > 0:
                cleaning["anomalies"] += bad_rows
                print(f"    ⚠️ {alabel}: {bad_rows} 行价格=0但有成交量", flush=True)
    except Exception as e:
        print(f"    异常检测失败: {e}", flush=True)

    # C4. 极异常涨跌幅检查（|pct_chg|>50%，排除新股首日）
    try:
        cur.execute(f"""
            SELECT COUNT(*) FROM daily_price
            WHERE trade_date=? AND ABS(pct_chg) > 50
            AND asset_type IN ('E', 'F', 'I')
        """, (latest_td,))
        extreme = cur.fetchone()[0]
        if extreme > 0:
            cleaning["anomalies"] += extreme
            print(f"    ⚠️ 极端涨跌幅(>50%): {extreme} 行", flush=True)
    except Exception as e:
        print(f"    极值检测失败: {e}", flush=True)

    result["cleaning"] = cleaning
    result["elapsed_sec"] = time.time() - start_time

    # 汇总
    total_issues = cleaning["duplicates_removed"] + cleaning["nulls_filled"] + cleaning["anomalies"]
    all_covered = all(c.get("ok", True) for c in coverage.values() if isinstance(c, dict))
    status_icon = "✅" if (all_covered and total_issues == 0) else "⚠️"

    print(f"\n  {status_icon} 质量检查完成: 覆盖度{'OK' if all_covered else '有缺失'} | "
          f"清洗: {cleaning['duplicates_removed']}重复/{cleaning['anomalies']}异常 | "
          f"回补: {fallback_pulled}只 | "
          f"⏱{result['elapsed_sec']:.1f}s", flush=True)

    return result


def sync_moneyflow_hsgt(db: DatabaseManager, trading_days: list,
                         latest_td: str,
                         force_backfill: bool = False) -> dict:
    """
    同步北向资金流向（Phase 10）

    从 pro.moneyflow_hsgt() 批拉取缺失交易日数据。
    force_backfill=True 时忽略已有数据，从2014-01-01起全量拉取。
    D3异常:
        API失败 → 跳过该日期
        空数据 → 正常跳过
    """
    import time
    t0 = time.time()
    result = {"rows_inserted": 0, "errors": 0, "elapsed_sec": 0.0}

    if force_backfill:
        # 全量回填：忽略已有数据，从2014-01-01起拉取
        missing = [d for d in trading_days if "20140101" <= d <= latest_td]
        print(f"  [10/13] 北向资金全量回填 — 从2014年至今, 共 {len(missing)} 天", flush=True)
    else:
        # 增量模式：获取DB最新日期
        try:
            cur = db.conn.cursor()
            cur.execute("SELECT MAX(trade_date) FROM moneyflow_hsgt")
            row = cur.fetchone()
            db_max = row[0] if row and row[0] else ""
        except Exception as e:
            print(f"  [10/13] 查询北向资金最新日期失败: {e}")
            db_max = ""

        missing = []
        if db_max:
            missing = [d for d in trading_days if db_max < d <= latest_td]
        else:
            # 空表：从2014-01-01起全量拉取
            missing = [d for d in trading_days if "20140101" <= d <= latest_td]

        if not missing:
            print(f"  [10/13] 北向资金 — 已是最新，跳过")
            result["elapsed_sec"] = round(time.time() - t0, 1)
            return result

    print(f"  [10/13] 北向资金 (moneyflow_hsgt) — 需要拉取 {len(missing)} 天", flush=True)

    from scripts.utils.tushare_client import pro as tushare_pro

    pulled = 0
    for i, td in enumerate(missing):
        try:
            df = tushare_pro.moneyflow_hsgt(start_date=td, end_date=td)
            if df is not None and not df.empty:
                row = df.iloc[0]
                data = {
                    "north_money": float(row.get("north_money", 0)),
                    "south_money": float(row.get("south_money", 0)),
                    "hgt": float(row.get("hgt", 0)),
                    "sgt": float(row.get("sgt", 0)),
                    "north_net": float(row.get("north_money", 0)),  # 无直接north_net，近似
                }
                if db.upsert_moneyflow_hsgt(td, data):
                    pulled += 1
        except Exception as e:
            result["errors"] += 1
            if result["errors"] <= 3:
                print(f"    [10/13] {td} 失败: {e}", flush=True)

        if (i + 1) % 50 == 0:
            print(f"    [10/13] {i+1}/{len(missing)}", flush=True)

        if (i + 1) < len(missing):
            import time as _t; _t.sleep(0.15)

    result["rows_inserted"] = pulled
    result["elapsed_sec"] = round(time.time() - t0, 1)
    print(f"  ✅ [10/13] moneyflow_hsgt: {pulled}天 ({result['elapsed_sec']:.0f}s)", flush=True)
    return result


def sync_moneyflow_stock_portfolio(db: DatabaseManager, latest_td: str,
                                   force_backfill: bool = False) -> dict:
    """
    同步持仓/自选股资金流向（Phase 11）

    读取 portfolio.json 和 watchlist.json，为持仓股拉取每日资金流向。

    模式:
        - 默认（force_backfill=False）: 仅拉取最新交易日数据
        - force_backfill=True: 全量历史回填（从2014-01-01起）

    D3异常:
        配置文件缺失 → 跳过，输出警告
        API失败 → 跳过该股票
        非股票代码（ETF/债券/可转债）→ 静默跳过
    """
    import time, json, re
    t0 = time.time()
    result = {"rows_inserted": 0, "errors": 0, "pulled_stocks": 0, "elapsed_sec": 0.0,
              "force_backfill": force_backfill}

    # 读取配置文件
    project_root = os.path.join(os.path.dirname(__file__), "..", "..")
    portfolio_path = os.path.join(project_root, "data", "portfolio.json")
    watchlist_path = os.path.join(project_root, "data", "watchlist.json")

    codes = set()

    def _extract_codes(data, fname=""):
        """递归从各种结构中抽取股票代码（支持中文/英文key）"""
        extracted = set()
        if isinstance(data, dict):
            holdings = data.get("持仓列表") or data.get("holdings") or data.get("positions")
            if isinstance(holdings, list):
                for item in holdings:
                    code = (item.get("代码") or item.get("ts_code") or item.get("code") or "")
                    if code and code.strip():
                        extracted.add(code.strip())
                if extracted:
                    return extracted
            for val in data.values():
                if isinstance(val, list):
                    for v in val:
                        if isinstance(v, str) and len(v) >= 6 and "." in v:
                            extracted.add(v)
            if extracted:
                return extracted
            for key in ("watchlist", "stocks", "holdings", "positions"):
                items = data.get(key, [])
                if isinstance(items, list):
                    for item in items:
                        code = (item.get("代码") or item.get("ts_code") or item.get("code") or "")
                        if code: extracted.add(code)
        elif isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    code = (item.get("代码") or item.get("ts_code") or item.get("code") or "")
                    if code: extracted.add(code)
                elif isinstance(item, str) and len(item) >= 6 and "." in item:
                    extracted.add(item)
        return extracted

    for fpath in (portfolio_path, watchlist_path):
        if os.path.exists(fpath):
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    data = json.load(f)
                codes.update(_extract_codes(data, os.path.basename(fpath)))
            except Exception as e:
                print(f"    [11/13] 读取 {os.path.basename(fpath)} 失败: {e}", flush=True)

    if not codes:
        print(f"  [11/13] 持仓资金流向 — 无持仓/自选股配置，跳过", flush=True)
        result["elapsed_sec"] = round(time.time() - t0, 1)
        return result

    # 过滤：仅保留A股股票代码（排除ETF 51xxxx/159xxx, 债券 11xxxx/12xxxx,
    #         可转债 11xxxx, 发债 718xxx/733xxx, 老三板 400xxx）
    def _is_stock(code):
        prefix = code[:3]
        return (prefix in ("000", "001", "002", "003", "004", "300", "301",
                           "600", "601", "603", "605", "688", "689") and
                "." in code)

    stock_codes = sorted(c for c in codes if _is_stock(c))
    skipped = sorted(c for c in codes if not _is_stock(c))
    if skipped:
        print(f"  [11/13] 跳过非股票代码: {', '.join(skipped[:10])}"
              f"{'...' if len(skipped) > 10 else ''}", flush=True)

    if not stock_codes:
        print(f"  [11/13] 持仓资金流向 — 无A股持仓（全为ETF/债券），跳过", flush=True)
        result["elapsed_sec"] = round(time.time() - t0, 1)
        return result

    mode_label = "全量历史回填" if force_backfill else "最新交易日"
    print(f"  [11/13] 持仓资金流向 (moneyflow_stock) — {len(stock_codes)} 只, "
          f"模式={mode_label}", flush=True)

    from scripts.utils.tushare_client import pro as tushare_pro

    pulled = 0
    start_date = "20140101" if force_backfill else latest_td

    for i, code in enumerate(stock_codes):
        try:
            df = tushare_pro.moneyflow(ts_code=code,
                                       start_date=start_date,
                                       end_date=latest_td)
            if df is not None and not df.empty:
                n = db.upsert_moneyflow_stock(df)
                if n > 0:
                    pulled += n
                    result["pulled_stocks"] += 1
        except Exception as e:
            result["errors"] += 1
            if result["errors"] <= 3:
                print(f"    [11/13] {code} 失败: {e}", flush=True)

        if (i + 1) < len(stock_codes):
            import time as _t; _t.sleep(0.12)

    result["rows_inserted"] = pulled
    result["elapsed_sec"] = round(time.time() - t0, 1)
    print(f"  ✅ [11/13] moneyflow_stock: {result['pulled_stocks']}只, {pulled}行 "
          f"({result['elapsed_sec']:.0f}s)", flush=True)
    return result


def sync_daily_indicators(db: DatabaseManager, latest_td: str,
                           lookback_days: int = 80) -> dict:
    """
    全市场技术指标增量计算（Phase 13）

    读取最近 lookback_days 天的价格数据，计算 MACD/KDJ/RSI/BOLL/MA 指标，
    写入 daily_indicator 表。

    参数:
        db: 数据库连接
        latest_td: 最新交易日 YYYYMMDD
        lookback_days: 回看天数
    返回:
        {"rows_inserted": int, "elapsed_sec": float}
    """
    import time
    t0 = time.time()
    result = {"rows_inserted": 0, "elapsed_sec": 0.0}

    print(f"  [13/13] 技术指标计算 — 回看 {lookback_days} 天...", flush=True)

    lookback_sql = f"""
        SELECT ts_code, trade_date, open, high, low, close, vol
        FROM daily_price
        WHERE trade_date >= date('now', '-{lookback_days} days')
        ORDER BY ts_code, trade_date ASC
    """

    df = pd.read_sql_query(lookback_sql, db.conn)
    if df.empty:
        print(f"  [13/13] 无新价格数据，跳过", flush=True)
        return result

    if "vol" in df.columns and "volume" not in df.columns:
        df = df.rename(columns={"vol": "volume"})

    from scripts.utils.technical_analysis import add_all_indicators

    all_rows = []
    total_stocks = df["ts_code"].nunique()
    processed = 0
    skipped = 0

    for code, grp in df.groupby("ts_code", sort=False):
        grp = grp.sort_values("trade_date").reset_index(drop=True)
        if len(grp) < 20:
            processed += 1
            skipped += 1
            continue

        try:
            df_ta = add_all_indicators(grp)
        except Exception:
            processed += 1
            continue

        if df_ta is None or df_ta.empty:
            processed += 1
            continue

        for _, row in df_ta.iterrows():
            vals = []
            for col in INDICATOR_COLS:
                v = row.get(col)
                if col in STR_COLS:
                    vals.append(str(v) if v is not None else None)
                elif col in BOOL_COLS:
                    vals.append(1 if v else 0)
                else:
                    if v is None or (isinstance(v, float) and np.isnan(v)):
                        vals.append(None)
                    else:
                        vals.append(float(v))
            all_rows.append(tuple(vals))

        processed += 1
        if processed % 200 == 0:
            print(f"    [13/13] {processed}/{total_stocks}, 累计 {len(all_rows)} 行",
                  flush=True)

    # 批量写入
    if all_rows:
        placeholders = ", ".join(["?" for _ in INDICATOR_COLS])
        cols_str = ", ".join(INDICATOR_COLS)
        upsert_sql = f"INSERT OR REPLACE INTO daily_indicator ({cols_str}) VALUES ({placeholders})"

        batch_size = 5000
        cur = db.conn.cursor()
        for i in range(0, len(all_rows), batch_size):
            batch = all_rows[i:i + batch_size]
            cur.executemany(upsert_sql, batch)
            db.conn.commit()
        result["rows_inserted"] = len(all_rows)

    elapsed = time.time() - t0
    result["elapsed_sec"] = round(elapsed, 1)

    print(f"  ✅ [13/13] daily_indicator 完成: {result['rows_inserted']}行 "
          f"(覆盖{total_stocks}只, 跳过{skipped}只, {elapsed:.0f}s)", flush=True)
    return result


# 常量定义（供 sync_daily_indicators 使用）
INDICATOR_COLS = [
    "ts_code", "trade_date",
    "macd", "macd_signal", "macd_diff",
    "macd_golden_cross", "macd_death_cross",
    "kdj_k", "kdj_d", "kdj_j", "kdj_golden_cross",
    "rsi_14", "rsi_oversold", "rsi_overbought",
    "boll_upper", "boll_mid", "boll_lower", "boll_width",
    "boll_break_upper", "boll_break_lower",
    "obv", "obv_ma20", "obv_trend", "obv_divergence",
    "ma_5", "ma_10", "ma_20", "ma_60",
]
BOOL_COLS = {"macd_golden_cross", "macd_death_cross", "kdj_golden_cross",
             "rsi_oversold", "rsi_overbought", "boll_break_upper", "boll_break_lower"}
STR_COLS = {"ts_code", "trade_date", "obv_trend", "obv_divergence"}


# ============================================================
#  主流程
# ============================================================

def run_sync(args) -> int:
    """
    主同步流程

    Returns:
        0: 成功（无需同步 或 同步完成）
        1: 部分成功
        2: 严重失败
    """
    print("=" * 70)
    print(f"  Auto-Sync 数据自动同步")
    print(f"  时间: {_now()}")
    mode = "检查模式 (--check-only)" if args.check_only else "自动同步 (--auto-sync)"
    print(f"  模式: {mode}")
    if args.auto_sync and args.type:
        print(f"  类型: {args.type}")
    print("=" * 70, flush=True)

    # === 1. 交易日历 ===
    trading_days = get_trading_calendar()
    if not trading_days:
        print("[ERROR] 无法获取交易日历，退出")
        return 2

    latest_td = get_latest_trading_day(trading_days)
    is_td = is_trading_day(trading_days)

    # === 2. 数据库 ===
    db = DatabaseManager()
    if not db._ensure_conn():
        print("[ERROR] 数据库连接失败")
        return 2

    stats_before = db.get_table_stats()

    # === 3. 新鲜度检查 ===
    freshness = check_freshness(db, trading_days)
    print_freshness_report(freshness)

    if args.check_only:
        db.close()
        return 0

    # === 4. 自动同步 ===
    if not is_td:
        # 指定类型时，允许部分回溯型类型在非交易日运行
        backfill_types = {"moneyflow_hsgt", "moneyflow_stock",
                          "fina_indicator", "dividend", "daily_indicator"}
        if not args.type or args.type not in backfill_types:
            print("今天不是交易日，跳过数据拉取。")
            db.close()
            return 0
        else:
            print("今天不是交易日，但指定了回溯类型，继续执行...")

    if freshness["overall_status"] == "fresh" and not args.type:
        print("所有数据已是最新，无需同步。")
        db.close()
        return 0

    # 加载全市场股票列表（只在需要时加载）
    if args.type and args.type in {"moneyflow_hsgt", "moneyflow_stock", "daily_indicator"}:
        all_codes = []
        print(f"指定类型 {args.type}，跳过全市场股票加载")
    else:
        all_codes = load_all_market_codes(db)
        if not all_codes:
            print("[ERROR] 无法加载股票列表")
            db.close()
            return 2
        print(f"全市场股票: {len(all_codes)} 只")

    # 时间预算
    max_minutes = getattr(args, 'max_minutes', 45) or 45
    total_budget = max_minutes * 60
    start_time = time.time()

    sync_results = {}

    # === Phase 0: fund_basic（ETF元数据，仅首次）===
    if args.type in (None, "all", "fund_basic"):
        print(f"\n{'='*60}")
        print(f"  [0/10] ETF元数据 (fund_basic)")
        print(f"{'='*60}")
        sync_results["fund_basic"] = sync_fund_basic(db)

    # === Phase 1: index_basic（指数元数据，仅首次）===
    if args.type in (None, "all", "index_basic"):
        print(f"\n{'='*60}")
        print(f"  [1/10] 指数元数据 (index_basic)")
        print(f"{'='*60}")
        sync_results["index_basic"] = sync_index_basic(db)

    # === Phase 2: daily_basic（最快）===
    if args.type in (None, "all", "daily_basic"):
        dbasic_info = freshness["tables"].get("daily_basic", {})
        dbasic_behind = dbasic_info.get("behind_trading_days", 0)

        if dbasic_behind > 0:
            dbasic_max = dbasic_info.get("max_date", "")
            if dbasic_max:
                # 找出缺失的交易日
                missing = [d for d in trading_days if dbasic_max < d <= latest_td]
            else:
                # 空表，从有 daily_price 数据的日期开始
                missing = [d for d in trading_days if d >= "20100101" and d <= latest_td]

            budget = total_budget - (time.time() - start_time)
            sync_results["daily_basic"] = sync_daily_basic(db, missing, max(budget, 30))
        else:
            print(f"\n  [2/10] 每日估值 — 已是最新，跳过")

    # === Phase 3: index_daily（全部指数日线）===
    if args.type in (None, "all", "index_daily"):
        # 获取指数在DB中的最新日期
        try:
            cur = db.conn.cursor()
            cur.execute("SELECT MAX(trade_date) FROM daily_price WHERE asset_type = 'I'")
            row = cur.fetchone()
            idx_max = row[0] if row and row[0] else ""
        except Exception as e:
            print(f"  [WARN] 指数日期查询失败: {e}")
            idx_max = ""

        idx_start = (datetime.strptime(idx_max, "%Y%m%d") + timedelta(days=1)).strftime("%Y%m%d") if idx_max else "19900101"

        if idx_start <= latest_td:
            budget = total_budget - (time.time() - start_time)
            sync_results["index_daily"] = sync_index_daily(db, idx_start, latest_td, max(budget, 60))
        else:
            print(f"\n  [3/10] 指数日线 — 已是最新，跳过")

    # === Phase 4: etf_daily（ETF日线行情）===
    if args.type in (None, "all", "etf_daily"):
        etf_info = freshness["tables"].get("etf_daily", {})
        etf_behind = etf_info.get("behind_trading_days", 0)

        if etf_behind > 0:
            etf_max = etf_info.get("max_date", "")
            etf_start = "19900101"
            if etf_max:
                etf_start = (datetime.strptime(etf_max, "%Y%m%d") + timedelta(days=1)).strftime("%Y%m%d")
            budget = total_budget - (time.time() - start_time)
            sync_results["etf_daily"] = sync_etf_daily(db, etf_start, latest_td, max(budget, 60))
        else:
            print(f"\n  [4/10] ETF日线 — 已是最新，跳过")

    # === Phase 5: daily_price（慢，主要内容）===
    if args.type in (None, "all", "daily_price"):
        dp_info = freshness["tables"].get("daily_price", {})
        dp_behind = dp_info.get("behind_trading_days", 0)
        dp_max = dp_info.get("max_date", "")

        if dp_behind > 0 and dp_max and latest_td:
            # 计算缺失的起始日期
            dp_start = (datetime.strptime(dp_max, "%Y%m%d") + timedelta(days=1)).strftime("%Y%m%d")

            # 找出滞后股票
            lagging = db.get_lagging_stocks("daily_price", latest_td)
            if lagging:
                budget = total_budget - (time.time() - start_time)
                max_stocks = getattr(args, 'max_stocks', 0) or 0
                sync_results["daily_price"] = sync_daily_price_incremental(
                    db, lagging, dp_start, latest_td,
                    max(budget, 60), max_stocks
                )
            else:
                print(f"\n  [5/10] 日线行情 — 所有股票已是最新，跳过")
        else:
            print(f"\n  [5/10] 日线行情 — 已是最新，跳过")

    # === Phase 6: adj_factor ===
    if args.type in (None, "all", "adj_factor"):
        adj_info = freshness["tables"].get("adj_factor", {})
        adj_behind = adj_info.get("behind_trading_days", 0)

        if adj_behind > 0:
            budget = total_budget - (time.time() - start_time)
            if budget > 30:
                sync_results["adj_factor"] = sync_adj_factor_incremental(
                    db, all_codes, budget
                )
            else:
                print(f"\n  [6/10] 复权因子 — 时间不足，跳过")
        else:
            print(f"\n  [6/10] 复权因子 — 已是最新，跳过")

    # === Phase 7: fina_indicator（季度）===
    if args.type in (None, "all", "fina_indicator"):
        fina_info = freshness["tables"].get("fina_indicator", {})
        fina_status = fina_info.get("status", "fresh")

        if fina_status == "stale":
            budget = total_budget - (time.time() - start_time)
            if budget > 60:
                sync_results["fina_indicator"] = sync_fina_indicator_incremental(
                    db, all_codes, budget
                )
            else:
                print(f"\n  [7/10] 财报数据 — 时间不足，跳过")
        else:
            print(f"\n  [7/10] 财报数据 — 无需更新，跳过")

    # === Phase 8: dividend ===
    if args.type in (None, "all", "dividend"):
        div_info = freshness["tables"].get("dividend", {})
        div_status = div_info.get("status", "fresh")

        if div_status == "stale":
            budget = total_budget - (time.time() - start_time)
            if budget > 60:
                sync_results["dividend"] = sync_dividend_incremental(
                    db, all_codes, budget
                )
            else:
                print(f"\n  [8/10] 分红数据 — 时间不足，跳过")
        else:
            print(f"\n  [8/10] 分红数据 — 无需更新，跳过")

    # === Phase 9: ths_daily (板块数据，东方财富/Tushare) ===
    if args.type in (None, "all", "ths_daily"):
        budget = total_budget - (time.time() - start_time)
        if budget > 10:  # 只需要很少时间（约6秒）
            print(f"\n  [9/13] 概念板块 (ths_daily)")
            try:
                from scripts.utils.sync_sector_moneyflow import sync_ths_daily
                sync_results["ths_daily"] = sync_ths_daily(db, latest_td)
            except Exception as e:
                print(f"  [9/13] 板块数据同步失败: {e}")
        else:
            print(f"\n  [9/13] 概念板块 — 时间不足，跳过")

    # === Phase 10: moneyflow_hsgt（北向资金）===
    if args.type in (None, "all", "moneyflow_hsgt"):
        force = args.type == "moneyflow_hsgt"  # 指定类型时强制全量回填
        # 全量回填需要更多时间预算
        min_budget = 30 if force else 10
        budget = total_budget - (time.time() - start_time)
        if budget > min_budget or force:
            sync_results["moneyflow_hsgt"] = sync_moneyflow_hsgt(
                db, trading_days, latest_td, force_backfill=force)
        else:
            print(f"\n  [10/13] 北向资金 — 时间不足，跳过")

    # === Phase 11: moneyflow_stock（持仓资金流向）===
    if args.type in (None, "all", "moneyflow_stock"):
        force_mf = args.type == "moneyflow_stock"  # 指定类型时强制全量回填
        budget = total_budget - (time.time() - start_time)
        if budget > (30 if force_mf else 20):
            sync_results["moneyflow_stock"] = sync_moneyflow_stock_portfolio(
                db, latest_td, force_backfill=force_mf)
        else:
            print(f"\n  [11/13] 持仓资金流向 — 时间不足，跳过")

    # === Phase 12: Post-Sync Quality Check ===
    if args.type in (None, "all", "quality_check"):
        if any(r.get("rows_inserted", 0) > 0 for r in sync_results.values() if isinstance(r, dict)):
            budget = total_budget - (time.time() - start_time)
            if budget > 30:
                qc_result = post_sync_data_check(db, trading_days, latest_td)
                sync_results["quality_check"] = qc_result
            else:
                print(f"\n  [12/13] 数据质量检查 — 时间不足，跳过")
        else:
            print(f"\n  [12/13] 数据质量检查 — 无新数据，跳过")

    # === Phase 13: daily_indicator（全市场技术指标计算）===
    if args.type in (None, "all", "daily_indicator"):
        budget = total_budget - (time.time() - start_time)
        if budget > 30:
            print(f"\n  [13/13] 全市场技术指标 (daily_indicator)")
            try:
                ind_result = sync_daily_indicators(db, latest_td)
                sync_results["daily_indicator"] = ind_result
            except Exception as e:
                print(f"  [13/13] 技术指标计算失败: {e}")
        else:
            print(f"\n  [13/13] 技术指标计算 — 时间不足，跳过")

    # === 汇总 ===
    elapsed_total = time.time() - start_time
    stats_after = db.get_table_stats()

    print(f"\n{'='*70}")
    print(f"  同步完成 — 总耗时 {elapsed_total/60:.1f} 分钟")
    print(f"{'='*70}")

    total_rows = 0
    for data_type, result in sync_results.items():
        rows = result.get("rows_inserted", 0)
        total_rows += rows
        if isinstance(result, dict) and rows > 0:
            pulled = result.get("pulled_stocks", result.get("pulled_dates", 0))
            errs = result.get("errors", 0)
            ela = result.get("elapsed_sec", 0)
            print(f"  {data_type}: {rows:,}行 ({pulled}成功/{errs}失败, {ela:.0f}s)")

    if total_rows == 0:
        print(f"\n  无新数据写入 — 数据已经是最新")

    # 行数变化
    print(f"\n  {'表名':<20} {'同步前':>10} {'同步后':>10} {'增量':>10}")
    print(f"  {'-'*54}")
    for table in sorted(stats_after.keys()):
        before = stats_before.get(table, 0)
        after = stats_after.get(table, 0)
        delta = after - before
        if delta != 0:
            print(f"  {table:<20} {before:>10,} {after:>10,} {delta:>+10,} ✅")
        elif after > 0:
            print(f"  {table:<20} {before:>10,} {after:>10,}      (无变化)")
        else:
            pass  # 空表不显示

    # 数据库文件大小
    db_path = os.path.join(PROJECT_ROOT, "data", "stocks.db")
    if os.path.exists(db_path):
        size_mb = os.path.getsize(db_path) / 1024 / 1024
        print(f"\n  数据库: {size_mb:.1f} MB | 总耗时: {elapsed_total/60:.1f} 分钟")

    db.close()

    # 判断返回码
    has_errors = any(r.get("errors", 0) > 0 for r in sync_results.values())
    return 1 if has_errors else 0


def main():
    parser = argparse.ArgumentParser(description="自动数据同步")
    parser.add_argument("--check-only", action="store_true",
                        help="仅检查数据新鲜度（不拉取数据，纯SQL，<1秒）")
    parser.add_argument("--auto-sync", action="store_true",
                        help="自动同步缺失数据")
    parser.add_argument("--type", type=str, default=None,
                        choices=["all", "daily_price", "adj_factor", "daily_basic",
                                 "fina_indicator", "dividend", "index_daily",
                                 "ths_daily", "daily_indicator",
                                 "moneyflow_hsgt", "moneyflow_stock"],
                        help="只同步特定类型（默认 all）")
    parser.add_argument("--max-minutes", type=int, default=30,
                        help="最大时间预算（分钟，默认30）")
    parser.add_argument("--max-stocks", type=int, default=0,
                        help="最多拉取股票数（0=全部，用于限速）")
    args = parser.parse_args()

    if not args.check_only and not args.auto_sync:
        # 默认：check-only（安全）
        args.check_only = True

    return run_sync(args)


if __name__ == "__main__":
    sys.exit(main())
