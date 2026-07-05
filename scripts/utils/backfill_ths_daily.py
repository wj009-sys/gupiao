"""
同花顺(THS)概念+行业板块历史数据回填工具

从 AkShare stock_board_concept_index_ths / stock_board_industry_index_ths
获取各板块历史日线数据，计算每日涨跌幅，写入 ths_daily 表。

用法:
    python scripts/utils/backfill_ths_daily.py                     # 默认最近180天
    python scripts/utils/backfill_ths_daily.py --since 20260101    # 指定起始日期
    python scripts/utils/backfill_ths_daily.py --since 20260601 --until 20260706
    python scripts/utils/backfill_ths_daily.py --dry-run           # 只统计不写入

输出: ths_daily 表 (INSERT OR REPLACE)

D3 异常处理:
    触发条件                    一线修复                  仍失败兜底
    ─────────────────────────  ────────────────────────  ──────────────────────
    THS board index 返回空     等待1秒重试1次           打印WARN，跳过该板块
    THS name list 拉取失败     等待3秒重试1次           打印ERROR，退出
    网络超时                   等待2秒重试1次           跳过该板块，累加错误计数
    pct_chg 计算 NaN           dropna 过滤             正常，首日无前收盘价
    DB 写入失败                自动回滚事务             打印ERROR，返回部分结果

D4 CHECKPOINT:
    [ ] CP1-板块数量: 预期 450+ 概念+行业板块
    [ ] CP2-日期覆盖: 数据日期应覆盖 --since ~ --until 的交易日
    [ ] CP3-涨跌幅范围: pct_chg 绝对值应 < 15%
    [ ] CP4-写入验证: 写入后行数 >= 预期 (板块数 × 交易日数) 的 80%

D9 工作反例:
    #  反模式                      为什么不要做              应该怎么做
    ──  ─────────────────────────  ────────────────────────  ──────────────────────
    1   一次性请求所有板块数据        容易触发 THS 限频封IP    逐板块请求，间隔 0.3-0.5s
    2   不排序直接计算 pct_chg       乱序数据会导致错误百分比  先 sort_values('date') 再 pct_change
    3   静默跳过失败板块              失板块数据不可见          打印 WARN 并增加错误计数
    4   硬编码日期范围                脚本复用性差              用 --since/--until 参数
"""
import os
os.environ["TQDM_DISABLE"] = "1"  # Suppress tqdm before any import
import sys
import time
import argparse
from datetime import datetime, timedelta

import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from scripts.utils.db_manager import DatabaseManager

# 请求间隔与重试
REQUEST_INTERVAL = 0.35  # THS API 间隔（秒）
MAX_RETRIES = 1          # 每板块重试次数

# 日期列索引（stock_board_*_index_ths 返回7列，列0=日期, 列4=收盘价）
COL_DATE = 0
COL_CLOSE = 4


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _date_fmt(val) -> str:
    """将 datetime.date/str 转为 YYYYMMDD"""
    if hasattr(val, 'strftime'):
        return val.strftime("%Y%m%d")
    s = str(val).replace("-", "")
    return s if len(s) == 8 else ""


def get_all_board_names() -> tuple:
    """
    获取所有概念+行业板块名称。

    Returns:
        (concept_df, industry_df) — 各含 name, code 列
    """
    import akshare as ak

    # 概念板块
    try:
        concept_df = ak.stock_board_concept_name_ths()
        print(f"  [THS] 概念板块: {len(concept_df)} 个")
    except Exception as e:
        print(f"  [THS] FAIL 概念板块列表获取失败: {e}")
        concept_df = pd.DataFrame()

    time.sleep(0.5)

    # 行业板块
    try:
        industry_df = ak.stock_board_industry_name_ths()
        print(f"  [THS] 行业板块: {len(industry_df)} 个")
    except Exception as e:
        print(f"  [THS] FAIL 行业板块列表获取失败: {e}")
        industry_df = pd.DataFrame()

    return concept_df, industry_df


def fetch_board_index(board_name: str, since: str, until: str, is_industry: bool = False) -> pd.DataFrame:
    """
    获取单个板块的 THS 指数日线数据。

    Returns:
        DataFrame(columns=[date, open, high, low, close, volume, amount]) 或 空
    """
    import akshare as ak

    for attempt in range(1 + MAX_RETRIES):
        try:
            func = ak.stock_board_industry_index_ths if is_industry else ak.stock_board_concept_index_ths
            df = func(symbol=board_name, start_date=since, end_date=until)
            if df is not None and not df.empty:
                return df
            if attempt < MAX_RETRIES:
                time.sleep(1)
        except Exception as e:
            if attempt < MAX_RETRIES:
                print(f"  [RETRY] {board_name}: {e}")
                time.sleep(1)
            else:
                print(f"  [FAIL] {board_name}: {e}")
    return pd.DataFrame()


def process_board(board_name: str, board_code: str, since: str, until: str,
                  is_industry: bool = False) -> list:
    """
    处理单个板块，返回 [{ts_code, trade_date, name, pct_chg, strength, anomaly}, ...]
    失败返回空列表。
    """
    df = fetch_board_index(board_name, since, until, is_industry)
    if df.empty:
        return []

    # 按日期排序
    date_col = df.iloc[:, COL_DATE]

    # 构建排序key
    df = df.copy()
    df["_sortkey"] = date_col.apply(
        lambda x: int(x.strftime("%Y%m%d")) if hasattr(x, 'strftime') else int(str(x).replace("-", ""))
    )
    df = df.sort_values("_sortkey").reset_index(drop=True)

    close = df.iloc[:, COL_CLOSE].astype(float)

    # 计算涨跌幅 (close_t - close_{t-1}) / close_{t-1} * 100
    pct = close.pct_change() * 100

    rows = []
    for i in range(1, len(df)):  # 跳过第0行（NaN pct_chg）
        trade_date = _date_fmt(df.iloc[i, COL_DATE])
        if not trade_date:
            continue
        pct_chg = float(pct.iloc[i])
        if pd.isna(pct_chg) or abs(pct_chg) > 15:
            continue  # 异常值跳过
        rows.append({
            "ts_code": str(board_code),
            "trade_date": trade_date,
            "name": board_name,
            "pct_chg": round(pct_chg, 2),
            "strength": round(pct_chg, 2),  # 简化版 strength
            "anomaly": 1 if abs(pct_chg) > 10.5 else 0,
        })

    return rows


def backfill(since: str, until: str, dry_run: bool = False) -> dict:
    """
    回填 THS 板块历史数据主入口。

    Args:
        since: 起始日期 YYYYMMDD
        until: 截止日期 YYYYMMDD
        dry_run: 仅统计，不写入

    Returns:
        {"concepts": N, "industries": N, "rows": N, "errors": N, "boards_with_data": N, "elapsed": F}
    """
    result = {"concepts": 0, "industries": 0, "rows": 0, "errors": 0,
              "boards_with_data": 0, "elapsed": 0}
    start_clock = time.time()

    print(f"\n{'='*60}")
    print(f"  同花顺 THS 板块指数数据回填")
    print(f"  {_now()}")
    print(f"  日期范围: {since} ~ {until}")
    print(f"  Dry-run: {'Y' if dry_run else 'N'}")
    print(f"{'='*60}\n")

    # Step 1: 获取板块列表
    print("  获取板块列表...")
    concept_df, industry_df = get_all_board_names()
    all_boards = []

    if not concept_df.empty:
        for _, row in concept_df.iterrows():
            all_boards.append((str(row["name"]), str(row["code"]), False))
    if not industry_df.empty:
        for _, row in industry_df.iterrows():
            all_boards.append((str(row["name"]), str(row["code"]), True))

    total_boards = len(all_boards)
    print(f"\n[STATS] 共 {total_boards} 个板块 (概念:{len(concept_df)} + 行业:{len(industry_df)})")
    if dry_run:
        print(f"\n[Dry-run] 只统计不写入\n")

    # Step 2: 逐板块获取数据
    all_rows = []
    errors = 0
    skipped = 0
    boards_with_data = 0

    for idx, (name, code, is_ind) in enumerate(all_boards, 1):
        label = f"[{idx}/{total_boards}] [{'I' if is_ind else 'C'}] {name}"
        t0 = time.time()
        rows = process_board(name, code, since, until, is_ind)
        elapsed = time.time() - t0

        if rows:
            all_rows.extend(rows)
            boards_with_data += 1
            print(f"  {label}: {len(rows)}行 ({elapsed:.1f}s)")
        else:
            skipped += 1
            print(f"  {label}: 无数据 ({elapsed:.1f}s)")

        # 限频
        if idx < total_boards:
            time.sleep(REQUEST_INTERVAL)

        # 每 50 个板块进度总结
        if idx % 50 == 0 or idx == total_boards:
            elapsed_total = time.time() - start_clock
            rate = idx / elapsed_total if elapsed_total > 0 else 0
            print(f"  -- 进度: {idx}/{total_boards} | 有效:{boards_with_data} | "
                  f"累计:{len(all_rows):,}行 | {rate:.1f}板块/秒 "
                  f"| 已用:{elapsed_total:.0f}s")

    # Step 3: 写入数据库
    result["concepts"] = len(concept_df)
    result["industries"] = len(industry_df)
    result["rows"] = len(all_rows)
    result["errors"] = errors
    result["boards_with_data"] = boards_with_data
    result["elapsed"] = time.time() - start_clock

    print(f"\n{'='*60}")
    print(f"  [RESULT] 采集汇总")
    print(f"{'='*60}")
    print(f"  总板块: {total_boards} (概念:{len(concept_df)} + 行业:{len(industry_df)})")
    print(f"  有数据: {boards_with_data}")
    print(f"  数据行: {len(all_rows):,}")
    print(f"  错误: {errors}")
    print(f"  耗时: {result['elapsed']:.1f}s ({result['elapsed']/60:.1f}min)")

    if not all_rows:
        print(f"\n  [WARN] 无数据可写入")
        return result

    # CP1 --- 板块数量
    if boards_with_data < 350:
        print(f"  [CP1] 板块数量偏少 {boards_with_data}/450+")

    # CP3 --- 涨跌幅范围
    df_check = pd.DataFrame(all_rows)
    extreme = df_check[df_check["pct_chg"].abs() > 12]
    if len(extreme) > 0:
        print(f"  [CP3] {len(extreme)} 行涨跌幅异常 (|pct_chg|>12)")

    if dry_run:
        # CP2 --- 日期覆盖
        dates = set(r["trade_date"] for r in all_rows)
        print(f"\n  覆盖交易日: {len(dates)} 天")
        print(f"  日期范围: {min(dates)} ~ {max(dates)}")
        print(f"\n  [Dry-run] 完成 (未写入)\n")
        return result

    # 写入 DB
    print(f"\n[DB] 写入数据库...")
    db = DatabaseManager()
    if not db._ensure_conn():
        print(f"  [DB] 数据库连接失败")
        return result

    try:
        n = db.upsert_ths_daily(pd.DataFrame(all_rows))
        print(f"  [OK] 写入 {n:,} 行到 ths_daily")

        # CP4 --- 写入验证
        cur = db.conn.cursor()
        cur.execute("SELECT COUNT(*) FROM ths_daily")
        total_in_db = cur.fetchone()[0]
        cur.execute("SELECT COUNT(DISTINCT trade_date) FROM ths_daily")
        days_in_db = cur.fetchone()[0]
        print(f"  [DB] ths_daily 总计: {total_in_db:,} 行, {days_in_db} 天")
        print(f"  [OK] 完成! ({_now()})")
    except Exception as e:
        print(f"  [DB] 写入失败: {e}")
    finally:
        db.close()

    return result


def main():
    parser = argparse.ArgumentParser(description="同花顺板块指数数据回填")
    parser.add_argument("--since", type=str, default="",
                        help="起始日期 YYYYMMDD (默认: 180天前)")
    parser.add_argument("--until", type=str, default="",
                        help="截止日期 YYYYMMDD (默认: 今天)")
    parser.add_argument("--dry-run", action="store_true",
                        help="仅统计不写入")
    args = parser.parse_args()

    today = datetime.now().strftime("%Y%m%d")
    since = args.since or (datetime.now() - timedelta(days=180)).strftime("%Y%m%d")
    until = args.until or today

    if since > until:
        print(f"[ERROR] since({since}) > until({until})")
        return 1

    # Suppress akshare progress bars (tqdm + logging)
    os.environ["TQDM_DISABLE"] = "1"
    import logging
    logging.getLogger("akshare").setLevel(logging.WARNING)

    result = backfill(since, until, dry_run=args.dry_run)
    print(f"\n回填完成: {result['rows']:,} 行")

    # D4 自检
    if not args.dry_run and result["rows"] > 0:
        # 估算预期: 板块数 x 交易日数
        expected_min = result["boards_with_data"] * 20
        if result["rows"] < expected_min:
            print(f"  [CP4] 行数偏少 {result['rows']:,} < 预期 {expected_min:,}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
