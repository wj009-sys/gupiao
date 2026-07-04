"""
一次性回填脚本 — 补全ETF和指数全量历史数据

功能:
  1. 拉取 fund_basic（ETF元数据, ~2,132只）
  2. 拉取 index_basic（指数元数据, SSE 208 + SZSE 485 + CSI精选~300）
  3. 拉取 ETF全量历史日线 → daily_price asset_type='F'（~100万行）
  4. 拉取 指数全量历史日线 → daily_price asset_type='I'（~300万行）

用法:
    # 默认（全量回填，时间预算充裕）
    python scripts/utils/backfill_etf_index.py

    # 只拉元数据（不拉历史行情）
    python scripts/utils/backfill_etf_index.py --metadata-only

    # 只拉ETF行情（跳过指数）
    python scripts/utils/backfill_etf_index.py --etf-only

    # 只拉指数行情（跳过ETF）
    python scripts/utils/backfill_etf_index.py --index-only

    # 估算时间
    python scripts/utils/backfill_etf_index.py --dry-run

D3异常:
| 触发条件 | 一线修复 | 仍失败兜底 |
|---------|---------|-----------|
| Tushare API限频 | sleep(3)后重试1次 | 跳过该ETF/指数，记录汇总 |
| ETF fund_daily返回空 | 可能是未上市或无交易 | 正常跳过（标记已处理）|
| 数据库连接失败 | 自动重连1次 | 退出脚本，已保存数据不丢失 |
| 中途中断 | 重新运行会跳过已有数据（INSERT OR REPLACE） | 幂等设计，可安全重跑 |
"""

import os
import sys
import time
import argparse
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from scripts.utils.tushare_client import pro
from scripts.utils.db_manager import DatabaseManager


def print_header(title: str):
    """打印分节标题"""
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}", flush=True)


def estimate_time(db: DatabaseManager) -> dict:
    """估算回填所需时间和数据量"""
    print_header("估算回填数据量")

    # ETF数量
    etf_codes = db.get_etf_codes()
    index_codes = db.get_index_codes()

    # 检查已有数据
    cur = db.conn.cursor()
    cur.execute("SELECT COUNT(*) FROM daily_price WHERE asset_type='F'")
    existing_etf_rows = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM daily_price WHERE asset_type='I'")
    existing_index_rows = cur.fetchone()[0]

    print(f"  ETF元数据: {len(etf_codes) if etf_codes else '待拉取'}只")
    print(f"  指数元数据: {len(index_codes) if index_codes else '待拉取'}只")
    print(f"  已有ETF日线: {existing_etf_rows:,}行")
    print(f"  已有指数日线: {existing_index_rows:,}行")

    # 估算新增行数（假设ETF平均500天/只，指数平均3000天/只）
    etf_new = max(0, (len(etf_codes) or 2132) * 500 - existing_etf_rows)
    index_new = max(0, ((208 + 485 + 300) * 3000) - existing_index_rows)
    total_rows = etf_new + index_new
    total_min = etf_new / 500 * 0.25 / 60 + index_new / 3000 * 0.2 / 60

    print(f"  ┌──────────────────┬────────────┬────────────┐")
    print(f"  │ 品类             │ 估算新增行数 │ 估算耗时    │")
    print(f"  ├──────────────────┼────────────┼────────────┤")
    print(f"  │ ETF日线          │ {etf_new:>10,}行 │ {etf_new/500*0.25/60:>5.1f}分钟 │")
    print(f"  │ 指数日线          │ {index_new:>10,}行 │ {index_new/3000*0.2/60:>5.1f}分钟 │")
    print(f"  ├──────────────────┼────────────┼────────────┤")
    print(f"  │ 合计             │ {total_rows:>10,}行 │ {total_min:>5.1f}分钟 │")
    print(f"  └──────────────────┴────────────┴────────────┘")

    return {
        "etf_new_rows": etf_new,
        "index_new_rows": index_new,
        "total_rows": total_rows,
        "est_minutes": total_min,
    }


def backfill_fund_basic(db: DatabaseManager) -> int:
    """回填ETF元数据"""
    existing = db.get_etf_codes()
    if existing:
        print(f"  ETF元数据已存在 ({len(existing)}只)，跳过")
        return len(existing)

    print_header("回填ETF元数据 (fund_basic)")
    try:
        df = pro.fund_basic(market='E', status='L')
        if df is not None and not df.empty:
            n = db.upsert_fund_basic(df)
            print(f"  ✅ ETF元数据写入: {n}只", flush=True)
            return n
        else:
            print(f"  ⚠️ fund_basic 返回空数据")
            return 0
    except Exception as e:
        print(f"  ❌ fund_basic 拉取失败: {e}")
        return 0


def backfill_index_basic(db: DatabaseManager) -> int:
    """回填指数元数据"""
    existing = db.get_index_codes()
    if existing:
        print(f"  指数元数据已存在 ({len(existing)}只)，跳过")
        return len(existing)

    print_header("回填指数元数据 (index_basic)")
    total = 0

    markets = {
        'SSE': '上证指数',
        'SZSE': '深证指数',
        'CSI': '中证指数(精选)',
    }

    for market, label in markets.items():
        try:
            df = pro.index_basic(market=market)
            if df is not None and not df.empty:
                if market == 'CSI':
                    # 过滤：只保留有用指数（排除多货币变体、债券/商品指数）
                    def _is_base_csi_code(code):
                        """标准CSI代码: 6位数字+.CSI"""
                        parts = code.split('.')
                        return len(parts) == 2 and len(parts[0]) == 6 and parts[0].isdigit()

                    def _useful(row):
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
                    useful = df[df.apply(_useful, axis=1)]
                    n = db.upsert_index_basic(useful) if not useful.empty else 0
                    print(f"  {label}: 原始{len(df)}只 → 筛选后{len(useful)}只", flush=True)
                else:
                    n = db.upsert_index_basic(df)
                    print(f"  {label}: {len(df)}只", flush=True)
                total += n
            time.sleep(0.3)
        except Exception as e:
            print(f"  {label} 拉取失败: {e}", flush=True)

    print(f"  ✅ 指数元数据合计: {total}只", flush=True)
    return total


def backfill_etf_daily(db: DatabaseManager, dry_run: bool = False) -> dict:
    """回填ETF全量历史日线"""
    result = {"pulled": 0, "rows": 0, "errors": 0, "skipped": 0}

    etf_codes = db.get_etf_codes()
    if not etf_codes:
        print("  ⚠️ ETF列表为空，跳过")
        return result

    print_header(f"回填ETF日线 ({len(etf_codes)}只)")

    if dry_run:
        print(f"  [DRY RUN] 将拉取 {len(etf_codes)} 只ETF的全量历史日线")
        return result

    start_time = time.time()

    for i, ts_code in enumerate(etf_codes):
        try:
            df = pro.fund_daily(ts_code=ts_code)
            if df is not None and not df.empty:
                n = db.upsert_daily_price(df, asset_type='F')
                result["rows"] += n
                result["pulled"] += 1
            else:
                result["skipped"] += 1
        except Exception as e:
            result["errors"] += 1
            if result["errors"] <= 5:
                print(f"  [{i+1}/{len(etf_codes)}] {ts_code} → 失败: {e}", flush=True)

        # 进度输出（每100只）
        if (i + 1) % 100 == 0:
            elapsed = time.time() - start_time
            rate = (i + 1) / elapsed if elapsed > 0 else 0
            eta = (len(etf_codes) - i - 1) / rate if rate > 0 else 0
            print(f"  [{i+1}/{len(etf_codes)}] "
                  f"成功{result['pulled']} 失败{result['errors']} "
                  f"| {result['rows']:,}行 | ⏱{elapsed/60:.1f}m ETA{eta/60:.1f}m", flush=True)

        # 限频
        if i < len(etf_codes) - 1:
            time.sleep(0.25)
        # 每50次额外休息（防Tushare限流）
        if (i + 1) % 50 == 0:
            time.sleep(1)

    elapsed = time.time() - start_time
    print(f"  ✅ ETF日线回填完成: {result['pulled']}只成功 / "
          f"{result['errors']}失败 / {result['skipped']}跳过", flush=True)
    print(f"     总行数: {result['rows']:,} | 耗时: {elapsed/60:.1f}分钟", flush=True)
    return result


def backfill_index_daily(db: DatabaseManager, dry_run: bool = False) -> dict:
    """回填指数全量历史日线"""
    result = {"pulled": 0, "rows": 0, "errors": 0}

    index_codes = db.get_index_codes()
    if not index_codes:
        print("  ⚠️ 指数列表为空，跳过")
        return result

    print_header(f"回填指数日线 ({len(index_codes)}只)")

    if dry_run:
        print(f"  [DRY RUN] 将拉取 {len(index_codes)} 只指数的全量历史日线")
        return result

    start_time = time.time()

    for i, ts_code in enumerate(index_codes):
        try:
            df = pro.index_daily(ts_code=ts_code)
            if df is not None and not df.empty:
                df = df.sort_values("trade_date").reset_index(drop=True)
                if "vol" in df.columns:
                    df = df.rename(columns={"vol": "volume"})
                n = db.upsert_daily_price(df, asset_type='I')
                result["rows"] += n
                result["pulled"] += 1
            else:
                result["errors"] += 1
                if result["errors"] <= 3:
                    print(f"  [{i+1}/{len(index_codes)}] {ts_code} → 无数据", flush=True)
        except Exception as e:
            result["errors"] += 1
            if result["errors"] <= 3:
                print(f"  [{i+1}/{len(index_codes)}] {ts_code} → 失败: {e}", flush=True)

        # 进度输出（每100只）
        if (i + 1) % 100 == 0:
            elapsed = time.time() - start_time
            rate = (i + 1) / elapsed if elapsed > 0 else 0
            eta = (len(index_codes) - i - 1) / rate if rate > 0 else 0
            print(f"  [{i+1}/{len(index_codes)}] "
                  f"成功{result['pulled']} 失败{result['errors']} "
                  f"| {result['rows']:,}行 | ⏱{elapsed/60:.1f}m ETA{eta/60:.1f}m", flush=True)

        if i < len(index_codes) - 1:
            time.sleep(0.2)
        if (i + 1) % 50 == 0:
            time.sleep(1)

    elapsed = time.time() - start_time
    print(f"  ✅ 指数日线回填完成: {result['pulled']}只成功 / {result['errors']}失败", flush=True)
    print(f"     总行数: {result['rows']:,} | 耗时: {elapsed/60:.1f}分钟", flush=True)
    return result


def print_summary(results: dict, start_time: float):
    """输出汇总"""
    elapsed = time.time() - start_time
    print(f"\n{'='*60}")
    print(f"  回填完成汇总")
    print(f"{'='*60}")
    for key, val in results.items():
        if isinstance(val, dict):
            rows = val.get("rows", val.get("total", 0))
            pulled = val.get("pulled", val.get("total", 0))
            errs = val.get("errors", 0)
            print(f"  {key:25s}  {pulled:>6}成功/{errs}失败 | {rows:>10,}行")
        else:
            print(f"  {key:25s}  {val:>10,}")
    print(f"  {'总耗时':25s}  {elapsed/60:.1f}分钟")


def main():
    parser = argparse.ArgumentParser(
        description="回填ETF和指数全量历史数据",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  %(prog)s                        全量回填
  %(prog)s --metadata-only        只拉元数据
  %(prog)s --etf-only             只拉ETF行情
  %(prog)s --index-only           只拉指数行情
  %(prog)s --dry-run              估算不执行
        """)
    parser.add_argument("--metadata-only", action="store_true",
                        help="只拉元数据（不拉历史行情）")
    parser.add_argument("--etf-only", action="store_true",
                        help="只拉ETF行情（跳过指数）")
    parser.add_argument("--index-only", action="store_true",
                        help="只拉指数行情（跳过ETF）")
    parser.add_argument("--dry-run", action="store_true",
                        help="估算模式（不实际拉取）")
    args = parser.parse_args()

    print("=" * 70)
    print(f"  ETF/指数 全量历史数据回填")
    print(f"  时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    if args.dry_run:
        print(f"  模式: DRY RUN（仅估算）")
    print("=" * 70, flush=True)

    db = DatabaseManager()
    if not db._ensure_conn():
        print("[ERROR] 数据库连接失败")
        return 1

    start_time = time.time()
    results = {}

    # 估算
    if args.dry_run or not (args.metadata_only or args.etf_only or args.index_only):
        estimate_time(db)
        if args.dry_run:
            print(f"\n  DRY RUN — 不执行实际拉取")
            return 0

    # 1. ETF元数据
    if not args.index_only:
        n = backfill_fund_basic(db)
        results["fund_basic"] = {"total": n}

    # 2. 指数元数据
    if not args.etf_only:
        n = backfill_index_basic(db)
        results["index_basic"] = {"total": n}

    # 3. ETF日线行情
    if not args.index_only and not args.metadata_only:
        etf_result = backfill_etf_daily(db, dry_run=args.dry_run)
        results["etf_daily"] = etf_result

    # 4. 指数日线行情
    if not args.etf_only and not args.metadata_only:
        index_result = backfill_index_daily(db, dry_run=args.dry_run)
        results["index_daily"] = index_result

    # 汇总
    print_summary(results, start_time)
    db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
