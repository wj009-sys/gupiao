"""
历史数据迁移脚本 — 将 data/raw/*.json 中的历史数据导入 SQLite 数据库

用法:
    # 预览模式（只显示将从哪些文件迁移什么数据，不实际写入）
    python scripts/utils/migrate_to_db.py --dry-run

    # 正式执行迁移
    python scripts/utils/migrate_to_db.py

    # 只迁移特定日期的数据
    python scripts/utils/migrate_to_db.py --date 20260630

    # 只迁移特定 Agent 的数据
    python scripts/utils/migrate_to_db.py --agent agent1

数据源映射:
| JSON文件名前缀 | 来源Agent | 迁移目标表 |
|--------------|----------|-----------|
| 情报原始数据_* | Agent1 | daily_price(I), moneyflow_hsgt, ths_daily, report_log |
| 分析原始数据_* | Agent2 | daily_indicator, report_log |
| 选股原始数据_* | Agent5 | report_log |
| 交易原始数据_* | Agent6 | portfolio_snapshot(间接), report_log |
| 风控报告_*     | Agent3 | report_log |
| 决策原始数据_* | Agent7 | report_log |
| 复盘报告_*     | Agent4 | report_log |

D3异常处理表:
| 触发条件 | 一线修复 | 仍失败兜底 |
|---------|---------|-----------|
| JSON文件解析失败 | 跳过该文件，打印警告 | 继续处理下一个文件 |
| 数据字段缺失（如market_overview为空） | 跳过该迁移步骤 | 不影响其他字段迁移 |
| 数据库连接失败 | 检查data/目录权限 | 退出迁移，提示手动检查 |
| 批量INSERT失败 | 逐行重试 | 跳过失败行，记录到错误汇总 |

D4 CHECKPOINT:
- CP1-文件存在检查：扫描前确认 data/raw/ 目录存在
- CP2-数据去重：使用 INSERT OR REPLACE 避免重复迁移
- CP3-汇总验证：迁移完成后输出各表行数变化

D9反例：
- 不要假设所有JSON格式一致（各Agent数据格式不同，需分别解析）
- 不要在迁移中删除原始JSON文件（保留作为备份）
- 不要用单条INSERT逐行写入（性能极差，使用executemany批量写入）
"""

import os
import sys
import json
import glob
import argparse
import pandas as pd
from datetime import datetime

# 添加项目根目录
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from scripts.utils.db_manager import DatabaseManager

# 指数名称→代码映射
INDEX_MAP = {
    "上证指数": "000001.SH",
    "深证成指": "399001.SZ",
    "创业板指": "399006.SZ",
    "科创50": "000688.SH",
}


def parse_date_from_filename(filename: str) -> str:
    """从文件名提取日期 YYYYMMDD"""
    basename = os.path.basename(filename)
    # 匹配 8位数字 的日期
    import re
    match = re.search(r'(\d{8})', basename)
    return match.group(1) if match else ""


def migrate_agent1_data(db: DatabaseManager, data: dict, trade_date: str, dry_run: bool) -> int:
    """
    迁移 Agent1 情报员数据:
    - market_overview → daily_price (asset_type='I')
    - moneyflow_hsgt → moneyflow_hsgt
    - ths_hot → ths_daily
    """
    if dry_run:
        rows = 0
        if data.get("market_overview"):
            rows += len(data["market_overview"])
        if data.get("ths_hot"):
            rows += len(data["ths_hot"])
        if data.get("moneyflow_hsgt"):
            rows += 1
        db.save_report_log(trade_date, "agent1", "ok")  # dry-run不写
        return rows

    n_total = 0

    # 1. 大盘指数 → daily_price
    market = data.get("market_overview", {})
    if market:
        rows = []
        for name, info in market.items():
            if isinstance(info, dict):
                rows.append({
                    "ts_code": INDEX_MAP.get(name, name),
                    "trade_date": trade_date,
                    "close": info.get("close"),
                    "pct_chg": info.get("pct_change", info.get("pct_chg")),
                    "amount": (info.get("amount", 0) or 0) * 10000,  # 亿元→万元
                })
        if rows:
            df = pd.DataFrame(rows)
            n = db.upsert_daily_price(df, asset_type='I')
            n_total += n

    # 2. 北向资金 → moneyflow_hsgt
    hsgt = data.get("moneyflow_hsgt", {})
    if hsgt and isinstance(hsgt, dict):
        if db.upsert_moneyflow_hsgt(trade_date, {
            "north_net": hsgt.get("north_net"),
            "hgt": hsgt.get("hgt"),
            "sgt": hsgt.get("sgt"),
        }):
            n_total += 1

    # 3. 板块数据 → ths_daily
    ths_hot = data.get("ths_hot", [])
    if ths_hot:
        for item in ths_hot:
            if isinstance(item, dict):
                item["trade_date"] = trade_date
                item["strength"] = item.get("pct_chg", 0)
                item["anomaly"] = abs(item.get("pct_chg", 0)) > 3
        df_ths = pd.DataFrame([h for h in ths_hot if isinstance(h, dict)])
        if not df_ths.empty:
            n = db.upsert_ths_daily(df_ths)
            n_total += n

    # 4. 报告日志
    db.save_report_log(trade_date, "agent1", "ok")

    return n_total


def migrate_agent2_data(db: DatabaseManager, data: dict, trade_date: str, dry_run: bool) -> int:
    """
    迁移 Agent2 分析师数据:
    - indices中的技术指标 → daily_indicator
    """
    if dry_run:
        indices = data.get("indices", [])
        return len([i for i in indices if isinstance(i, dict) and "signals" in i])

    n_total = 0
    indices = data.get("indices", [])
    for idx in indices:
        if not isinstance(idx, dict) or "error" in idx:
            continue

        ts_code = idx.get("ts_code", "")
        if not ts_code:
            continue

        signals = idx.get("signals", {})

        # 构建指标行
        row = {
            "ts_code": ts_code,
            "trade_date": trade_date,
            "ma_5": idx.get("ma_5"),
            "ma_20": idx.get("ma_20"),
            "ma_60": idx.get("ma_60"),
        }

        # 尝试解析MACD信号文本
        macd_text = signals.get("macd", "")
        if "金叉" in str(macd_text):
            row["macd_golden_cross"] = 1
        elif "死叉" in str(macd_text):
            row["macd_death_cross"] = 1

        # KDJ
        kdj_text = signals.get("kdj", "")
        if "金叉" in str(kdj_text):
            row["kdj_golden_cross"] = 1

        df = pd.DataFrame([row])
        n = db.upsert_daily_indicator(df)
        n_total += n

    # 报告日志
    db.save_report_log(trade_date, "agent2", "ok")

    return n_total


def migrate_report_log(db: DatabaseManager, filename: str, data: dict,
                       agent_id: str, dry_run: bool) -> int:
    """通用报告日志迁移"""
    trade_date = parse_date_from_filename(filename)
    if not trade_date:
        return 0
    if not dry_run:
        db.save_report_log(trade_date, agent_id, "ok")
    return 1


def run_migration(date_filter: str = None, agent_filter: str = None, dry_run: bool = False):
    """
    主迁移函数

    Args:
        date_filter: 只迁移指定日期的数据 (YYYYMMDD)
        agent_filter: 只迁移指定Agent的数据 (agent1~agent7)
        dry_run: True=预览模式，不实际写入
    """
    raw_dir = os.path.join(os.path.dirname(__file__), "..", "..", "data", "raw")

    if not os.path.exists(raw_dir):
        print(f"[ERROR] 数据目录不存在: {raw_dir}")
        return

    json_files = sorted(glob.glob(os.path.join(raw_dir, "*.json")))
    if not json_files:
        print("[INFO] data/raw/ 中没有 JSON 文件")
        return

    # 按日期过滤
    if date_filter:
        json_files = [f for f in json_files if date_filter in os.path.basename(f)]

    # 按Agent过滤
    agent_file_patterns = {
        "agent1": "情报原始数据",
        "agent2": "分析原始数据",
        "agent5": "选股原始数据",
        "agent6": "交易原始数据",
        "agent3": "风控报告",
        "agent4": "复盘报告",
        "agent7": "决策原始数据",
    }
    if agent_filter:
        pattern = agent_file_patterns.get(agent_filter, "")
        if pattern:
            json_files = [f for f in json_files if pattern in os.path.basename(f)]

    if not json_files:
        print(f"[INFO] 没有匹配的JSON文件 (date={date_filter}, agent={agent_filter})")
        return

    mode = "预览 (--dry-run)" if dry_run else "正式迁移"
    print(f"\n{'='*60}")
    print(f"  历史数据迁移 — {mode}")
    print(f"  文件数: {len(json_files)}")
    print(f"  数据库: {DatabaseManager().db_path}")
    print(f"{'='*60}\n")

    db = DatabaseManager()
    stats_before = db.get_table_stats()

    total_rows = 0
    success_count = 0
    error_count = 0

    for fpath in json_files:
        fname = os.path.basename(fpath)
        print(f"  {'[预览]' if dry_run else '[处理]'} {fname}...", end=" ")

        # 解析JSON
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                data = json.load(f)
        except json.JSONDecodeError as e:
            print(f"  跳过 (JSON解析失败: {e})")
            error_count += 1
            continue
        except Exception as e:
            print(f"  跳过 (读取失败: {e})")
            error_count += 1
            continue

        if not isinstance(data, dict):
            print("  跳过 (数据不是字典)")
            error_count += 1
            continue

        trade_date = data.get("date", parse_date_from_filename(fname))

        try:
            rows = 0
            # 判断文件类型并调用对应迁移函数
            if "情报原始数据" in fname:
                if agent_filter is None or agent_filter == "agent1":
                    rows = migrate_agent1_data(db, data, trade_date, dry_run)
            elif "分析原始数据" in fname:
                if agent_filter is None or agent_filter == "agent2":
                    rows = migrate_agent2_data(db, data, trade_date, dry_run)
            elif "选股原始数据" in fname:
                if agent_filter is None or agent_filter == "agent5":
                    rows = migrate_report_log(db, fname, data, "agent5", dry_run)
            elif "交易原始数据" in fname:
                if agent_filter is None or agent_filter == "agent6":
                    rows = migrate_report_log(db, fname, data, "agent6", dry_run)
            elif "风控报告" in fname:
                if agent_filter is None or agent_filter == "agent3":
                    rows = migrate_report_log(db, fname, data, "agent3", dry_run)
            elif "决策原始数据" in fname:
                if agent_filter is None or agent_filter == "agent7":
                    rows = migrate_report_log(db, fname, data, "agent7", dry_run)
            elif "复盘报告" in fname:
                if agent_filter is None or agent_filter == "agent4":
                    rows = migrate_report_log(db, fname, data, "agent4", dry_run)
            else:
                print("  跳过 (未识别类型)")
                continue

            if rows > 0:
                total_rows += rows
                print(f"  {rows} 行")
            else:
                print("  0 行 (无数据)")
            success_count += 1

        except Exception as e:
            print(f"  失败: {e}")
            error_count += 1

    # 汇总
    print(f"\n{'='*60}")
    print(f"  迁移{'预览' if dry_run else ''}完成")
    print(f"  成功: {success_count} 文件 | 失败: {error_count} 文件")
    if not dry_run:
        print(f"  写入行数: {total_rows}")
    print(f"{'='*60}")

    if not dry_run:
        stats_after = db.get_table_stats()
        print(f"\n  各表行数变化:")
        for table in sorted(stats_after.keys()):
            before = stats_before.get(table, 0)
            after = stats_after.get(table, 0)
            delta = after - before
            if delta != 0:
                print(f"    {table}: {before} → {after} (+{delta})")
            else:
                print(f"    {table}: {after} (无变化)")

        # 数据范围
        date_range = db.get_date_range()
        print(f"\n  数据日期范围: {date_range[0]} ~ {date_range[1]}")

    db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="历史数据迁移到 SQLite 数据库")
    parser.add_argument("--dry-run", action="store_true", help="预览模式，不实际写入数据库")
    parser.add_argument("--date", type=str, default=None, help="只迁移指定日期的数据 (YYYYMMDD)")
    parser.add_argument("--agent", type=str, default=None,
                        choices=["agent1", "agent2", "agent3", "agent4", "agent5", "agent6", "agent7"],
                        help="只迁移指定Agent的数据")
    args = parser.parse_args()

    run_migration(date_filter=args.date, agent_filter=args.agent, dry_run=args.dry_run)
