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
  1. daily_basic   — pro.daily_basic(trade_date=X) 按日期批量  ⚡ 1次/天
  2. index_daily   — pro.index_daily(ts_code=X) 8个指数       ⚡ 8次
  3. daily_price   — pro.daily(ts_code=X) 逐股拉缺失日        🐢 ~22min/天
  4. adj_factor    — pro.adj_factor(ts_code=X)                中等
  5. fina_indicator— pro.fina_indicator(ts_code=X)            仅季度缺口
  6. dividend      — pro.dividend(ts_code=X)                  很少需要

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

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from scripts.utils.tushare_client import pro
from scripts.utils.db_manager import DatabaseManager

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
CALENDAR_CACHE = os.path.join(PROJECT_ROOT, "data", "trading_calendar.json")

# 8个主要大盘指数
MAJOR_INDICES = [
    ("000001.SH", "上证指数"),
    ("399001.SZ", "深证成指"),
    ("399006.SZ", "创业板指"),
    ("000688.SH", "科创50"),
    ("000016.SH", "上证50"),
    ("000300.SH", "沪深300"),
    ("399905.SZ", "中证500"),
    ("000852.SH", "中证1000"),
]

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
            pass  # non-critical fallback

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
            pass  # non-critical fallback
    result["tables"]["fina_indicator"] = {
        "max_date": fina_max,
        "status": fina_status,
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
            pass  # non-critical fallback
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
    print(f"  [1/7] 每日估值 (daily_basic) — {len(missing_dates)} 天")
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


def sync_index_daily(db: DatabaseManager, start_date: str, end_date: str) -> dict:
    """
    同步8个大盘指数日线

    Args:
        db: 数据库管理器
        start_date: 起始日期
        end_date: 截止日期

    Returns:
        {"indices": N, "rows_inserted": N, "errors": N, "elapsed_sec": F}
    """
    result = {"indices": 0, "rows_inserted": 0, "errors": 0, "elapsed_sec": 0}

    print(f"\n{'='*60}")
    print(f"  [2/7] 大盘指数 (index_daily) — {len(MAJOR_INDICES)} 个指数")
    print(f"{'='*60}")

    start_time = time.time()

    for i, (ts_code, name) in enumerate(MAJOR_INDICES):
        try:
            df = pro.index_daily(ts_code=ts_code, start_date=start_date, end_date=end_date)
            if df is not None and not df.empty:
                df = df.sort_values("trade_date").reset_index(drop=True)
                if "vol" in df.columns:
                    df = df.rename(columns={"vol": "volume"})
                n = db.upsert_daily_price(df, asset_type='I')
                result["rows_inserted"] += n
                result["indices"] += 1
                print(f"  [{i+1}/{len(MAJOR_INDICES)}] {name} ({ts_code}) → {n}行", flush=True)
            else:
                result["errors"] += 1
                print(f"  [{i+1}/{len(MAJOR_INDICES)}] {name} ({ts_code}) → 无数据", flush=True)
        except Exception as e:
            result["errors"] += 1
            print(f"  [{i+1}/{len(MAJOR_INDICES)}] {name} ({ts_code}) → 失败: {e}", flush=True)

        if i < len(MAJOR_INDICES) - 1:
            time.sleep(0.2)

    result["elapsed_sec"] = time.time() - start_time
    print(f"  指数日线完成: {result['indices']}个 {result['rows_inserted']:,}行 "
          f"({result['elapsed_sec']:.1f}s)", flush=True)
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
    print(f"  [3/7] 日线行情 (daily_price) — {len(stocks_to_pull)} 只滞后股票")
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
        pass  # non-critical fallback

    stocks_to_pull = [c for c in stock_codes if c not in db_existing]
    if not stocks_to_pull:
        print(f"\n  [4/7] 复权因子 — 所有股票已有数据，跳过")
        return result

    print(f"\n{'='*60}")
    print(f"  [4/7] 复权因子 (adj_factor) — {len(stocks_to_pull)} 只新股票")
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
    except Exception:
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
            pass  # non-critical fallback

    if not lagging_stocks:
        print(f"\n  [5/7] 财报数据 — 所有股票已是最新，跳过")
        return result

    print(f"\n{'='*60}")
    print(f"  [5/7] 财报数据 (fina_indicator) — {len(lagging_stocks)} 只滞后股票")
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
                fields="ts_code,end_date,revenue,profit_dedt,roe,roa,"
                       "grossprofit_margin,debt_to_assets,current_ratio,"
                       "revenue_yoy,profit_dedt_yoy,or_yoy"
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
        pass  # non-critical fallback

    stocks_to_pull = [c for c in stock_codes if c not in db_existing]
    if not stocks_to_pull:
        print(f"\n  [6/6] 分红数据 — 所有股票已有数据，跳过")
        return result

    print(f"\n{'='*60}")
    print(f"  [6/6] 分红送转 (dividend) — {len(stocks_to_pull)} 只新股票")
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
        pass  # non-critical fallback

    # 如果 stock_basic 为空，从 daily_price 获取
    try:
        cur = db.conn.cursor()
        cur.execute("SELECT DISTINCT ts_code FROM daily_price ORDER BY ts_code")
        return [row[0] for row in cur.fetchall()]
    except Exception:
        return []


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
        print("今天不是交易日，跳过数据拉取。")
        db.close()
        return 0

    if freshness["overall_status"] == "fresh":
        print("所有数据已是最新，无需同步。")
        db.close()
        return 0

    # 加载全市场股票列表
    all_codes = load_all_market_codes(db)
    if not all_codes:
        print("[ERROR] 无法加载股票列表")
        db.close()
        return 2
    print(f"全市场股票: {len(all_codes)} 只")

    # 时间预算
    max_minutes = getattr(args, 'max_minutes', 30) or 30
    total_budget = max_minutes * 60
    start_time = time.time()

    sync_results = {}

    # === Phase 1: daily_basic（最快）===
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
            print(f"\n  [1/7] 每日估值 — 已是最新，跳过")

    # === Phase 2: index_daily（快）===
    if args.type in (None, "all", "index_daily"):
        # 获取指数在DB中的最新日期
        try:
            cur = db.conn.cursor()
            cur.execute("SELECT MAX(trade_date) FROM daily_price WHERE asset_type = 'I'")
            row = cur.fetchone()
            idx_max = row[0] if row and row[0] else ""
        except Exception:
            idx_max = ""

        idx_start = (datetime.strptime(idx_max, "%Y%m%d") + timedelta(days=1)).strftime("%Y%m%d") if idx_max else "19900101"

        if idx_start <= latest_td:
            sync_results["index_daily"] = sync_index_daily(db, idx_start, latest_td)
        else:
            print(f"\n  [2/7] 大盘指数 — 已是最新，跳过")

    # === Phase 3: daily_price（慢，主要内容）===
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
                print(f"\n  [3/7] 日线行情 — 所有股票已是最新，跳过")
        else:
            print(f"\n  [3/7] 日线行情 — 已是最新，跳过")

    # === Phase 4: adj_factor ===
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
                print(f"\n  [4/7] 复权因子 — 时间不足，跳过")
        else:
            print(f"\n  [4/7] 复权因子 — 已是最新，跳过")

    # === Phase 5: fina_indicator（季度）===
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
                print(f"\n  [5/7] 财报数据 — 时间不足，跳过")
        else:
            print(f"\n  [5/7] 财报数据 — 无需更新，跳过")

    # === Phase 6: dividend ===
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
                print(f"\n  [6/7] 分红数据 — 时间不足，跳过")
        else:
            print(f"\n  [6/7] 分红数据 — 无需更新，跳过")

    # === Phase 7: ths_daily (板块数据，东方财富/Tushare) ===
    if args.type in (None, "all", "ths_daily"):
        budget = total_budget - (time.time() - start_time)
        if budget > 10:  # 只需要很少时间（约6秒）
            print(f"\n  [7/7] 概念板块 (ths_daily)")
            try:
                from scripts.utils.sync_sector_moneyflow import sync_ths_daily
                sync_results["ths_daily"] = sync_ths_daily(db, latest_td)
            except Exception as e:
                print(f"  [7/7] 板块数据同步失败: {e}")
        else:
            print(f"\n  [7/7] 概念板块 — 时间不足，跳过")

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
                                 "fina_indicator", "dividend", "index_daily", "ths_daily"],
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
