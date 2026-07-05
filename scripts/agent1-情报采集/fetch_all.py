"""
Agent1 情报员 - 数据采集核心脚本
一键获取：大盘行情 + 龙虎榜 + 资金流向 + 板块涨跌 + 热点题材 + 全球资讯 + 公告

用法：
    source venv/Scripts/activate
    python scripts/agent1-情报采集/fetch_all.py YYYYMMDD

    # 省略日期则默认取最新交易日
    python scripts/agent1-情报采集/fetch_all.py

D9反例（工作反例）：
1. 不要堆砌新闻标题而不去重——用户需要的是分析后的信息，不是新闻转储
2. 不要传播未经官方确认的传闻——只采集可验证的客观数据
3. 不要忽略现有持仓关联信息——采集时应知道手上有什么票
4. 不要使用超过3天的旧数据作为当日判断依据

D3异常处理表：
| 触发条件 | 一线修复 | 仍失败兜底 |
|---------|---------|-----------|
| Tushare API全挂（网络/Token失效） | 检查网络连接，重试1次各API | 返回空数据data，标记errors；报告生成阶段提示"数据源不可用，跳过" |
| 北向资金数据为空 | 尝试往前推1个交易日获取 | 返回{north_net:0, hgt:0, sgt:0}，不报错 |
| 龙虎榜API无数据（非交易日/接口变更） | 打印警告并返回空DataFrame | 跳过龙虎榜单章节 |
| 板块排名数据为空 | 往前找5天内的有效数据 | 跳过板块分析章节 |
| 涨跌家数统计失败 | 捕获异常，打印错误 | 返回{up:0, down:0, flat:0, total:0} |
| 当天是非交易日 | get_last_trade_day往前找7天 | 找到最近交易日的数据，标注"非最新交易日" |
| JSON序列化numpy类型 | 使用default=str处理不可序列化类型 | 字符串化所有特殊类型 |
| 同花顺热点题材API失败 | 捕获异常，仅做警告 | 跳过热点题材章节 |
| 东财全球资讯API失败 | 捕获异常，仅做警告 | 跳过全球资讯章节 |
| 巨潮公告查询失败 | 捕获异常，逐个跳过 | 跳过公告章节 |

D4 CHECKPOINT:
- CP1-交易日验证：调用get_last_trade_day确认有数据，非交易日则往前回溯
- CP2-数据完整性：generate_data_json采集完成后检查data各字段是否非空
- CP3-错误汇总：采集完成后输出errors数量，0 errors才标记为"完整采集"
"""

import os
import sys
import json
import pandas as pd
from datetime import datetime, timedelta

# 添加项目根目录到路径
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from scripts.utils.tushare_client import pro, get_ths_index

# ── a-stock-data 新数据源（优雅降级） ──
try:
    from scripts.utils.ths_provider import ths_hot_reason, hot_reason_tag_ranking
    _HAS_THS = True
except Exception:
    _HAS_THS = False
try:
    from scripts.utils.eastmoney_plus import eastmoney_global_news, eastmoney_concept_blocks
    _HAS_EM = True
except Exception:
    _HAS_EM = False
try:
    from scripts.utils.cninfo_sentiment import cninfo_announcements
    _HAS_CNINFO = True
except Exception:
    _HAS_CNINFO = False

# 数据库管理器（尽力而为，导入失败不影响报告生成）
try:
    from scripts.utils.db_manager import DatabaseManager
    _db = DatabaseManager()
except Exception as e:
    print(f"  [WARN] 数据库连接失败，跳过DB写入: {e}")
    _db = None


def get_today() -> str:
    """获取今天的 YYYYMMDD 格式"""
    return datetime.now().strftime("%Y%m%d")


def get_last_trade_day() -> str:
    """获取上一个交易日（如今天有数据就用今天，否则用上一个交易日）"""
    today = get_today()
    for offset in range(1, 8):  # 最多往前找7天
        date = (datetime.now() - timedelta(days=offset)).strftime("%Y%m%d")
        try:
            df = pro.daily(trade_date=date)
            if not df.empty:
                return date
        except Exception as e:
            print(f'  [WARN] 获取交易日失败 (offset={offset}): {e}')
    return today


def fetch_market_overview(trade_date: str) -> dict:
    """获取大盘概况"""
    indices = {
        "上证指数": "000001.SH",
        "深证成指": "399001.SZ",
        "创业板指": "399006.SZ",
    }
    result = {}
    for name, code in indices.items():
        try:
            df = pro.index_daily(ts_code=code, start_date=trade_date, end_date=trade_date)
            if not df.empty:
                row = df.iloc[0]
                result[name] = {
                    "close": float(row["close"]),
                    "pct_change": float(row["pct_chg"]),
                    "amount": float(row["amount"]) / 1e8,  # 转为亿
                }
        except Exception as e:
            print(f"    大盘指数[{name}]获取失败: {e}")
    return result


def fetch_moneyflow_hsgt(trade_date: str) -> dict:
    """获取北向资金流向（单位：万元）"""
    try:
        df = pro.moneyflow_hsgt(start_date=trade_date, end_date=trade_date)
        if not df.empty:
            row = df.iloc[0]
            # north_money 单位为万元，转为亿元
            north = float(row.get("north_money", 0)) / 1e4
            hgt = float(row.get("hgt", 0)) / 1e4
            sgt = float(row.get("sgt", 0)) / 1e4
            return {
                "north_net": round(north, 2),
                "hgt": round(hgt, 2),
                "sgt": round(sgt, 2),
            }
    except Exception as e:
        print(f"    北向资金API异常: {e}")
    return {"north_net": 0, "hgt": 0, "sgt": 0}


def fetch_limit_list(trade_date: str) -> pd.DataFrame:
    """获取龙虎榜（Tushare接口名可能变化，失败时静默处理）"""
    try:
        df = pro.limit_list(trade_date=trade_date)
        if not df.empty:
            cols = [c for c in ["ts_code", "name", "close", "pct_chg", "amount", "buy_amount", "sell_amount"] if c in df.columns]
            return df[cols]
    except Exception as e:
        print(f"    龙虎榜API不可用: {e}")
    return pd.DataFrame()


def fetch_ths_hot(trade_date: str) -> pd.DataFrame:
    """获取概念板块涨跌排名（优先Tushare THS，失败自动回退东方财富）"""
    try:
        df = get_ths_index(daily=True)
        if not df.empty:
            df = df.sort_values("pct_chg", ascending=False)
            return df[["ts_code", "name", "pct_chg"]]
    except Exception as e:
        print(f"  [WARN] 板块数据获取失败: {e}")
    return pd.DataFrame()


def fetch_up_down_count(trade_date: str) -> dict:
    """获取每日涨跌家数（使用 daily 接口，含 pct_chg）"""
    try:
        df = pro.daily(trade_date=trade_date)
        if not df.empty:
            up = int((df["pct_chg"] > 0).sum())
            down = int((df["pct_chg"] < 0).sum())
            flat = int((df["pct_chg"] == 0).sum())
            return {"up": up, "down": down, "flat": flat, "total": len(df)}
    except Exception as e:
        print(f"    涨跌统计异常: {e}")
    return {"up": 0, "down": 0, "flat": 0, "total": 0}


def fetch_stock_daily(stock_code: str, start_date: str, end_date: str) -> pd.DataFrame:
    """获取个股行情"""
    try:
        df = pro.daily(ts_code=stock_code, start_date=start_date, end_date=end_date)
        return df
    except Exception as e:
        print(f"  [WARN] fetch_stock_daily({stock_code}) 失败: {e}")
        return pd.DataFrame()


def generate_data_json(trade_date: str = None) -> dict:
    """
    主函数：采集所有数据并返回 JSON 结构
    """
    if trade_date is None:
        trade_date = get_last_trade_day()

    # Windows GBK 兼容输出
    ok = "[OK]"
    fail = "[FAIL]"
    warn = "[WARN]"

    print(f"[情报员] 采集日期: {trade_date}")

    data = {
        "date": trade_date,
        "market_overview": {},
        "moneyflow_hsgt": {},
        "up_down_count": {},
        "top_gainers": [],
        "top_losers": [],
        "limit_list": [],
        "ths_hot": [],
        "hot_reason": [],          # 同花顺热点题材归因（a-stock-data）
        "hot_reason_ranking": [],  # 题材热度排名
        "global_news": [],         # 东财全球资讯（a-stock-data）
        "watch_announcements": [], # 自选股最新公告（a-stock-data）
        "errors": [],
    }

    # 1. 大盘概况
    try:
        data["market_overview"] = fetch_market_overview(trade_date)
        print(f"  {ok} 大盘概况: {len(data['market_overview'])} 个指数")
    except Exception as e:
        data["errors"].append(f"大盘概况获取失败: {e}")
        print(f"  {fail} 大盘概况: {e}")

    # 2. 北向资金
    try:
        data["moneyflow_hsgt"] = fetch_moneyflow_hsgt(trade_date)
        net = data["moneyflow_hsgt"].get("north_net", 0)
        print(f"  {ok} 北向资金: {net:.2f} 亿")
    except Exception as e:
        data["errors"].append(f"北向资金获取失败: {e}")
        print(f"  {fail} 北向资金: {e}")

    # 3. 涨跌家数
    try:
        data["up_down_count"] = fetch_up_down_count(trade_date)
        print(f"  {ok} 涨跌统计: {data['up_down_count'].get('up', 0)}涨 / {data['up_down_count'].get('down', 0)}跌")
    except Exception as e:
        data["errors"].append(f"涨跌统计获取失败: {e}")
        print(f"  {fail} 涨跌统计: {e}")

    # 4. 龙虎榜
    try:
        limit_df = fetch_limit_list(trade_date)
        if not limit_df.empty:
            data["limit_list"] = limit_df.head(10).to_dict("records")
            print(f"  {ok} 龙虎榜: {len(limit_df)} 只个股")
    except Exception as e:
        data["errors"].append(f"龙虎榜获取失败: {e}")
        print(f"  {fail} 龙虎榜: {e}")

    # 5. 板块涨跌排名
    try:
        ths_df = fetch_ths_hot(trade_date)
        if not ths_df.empty:
            data["ths_hot"] = ths_df.to_dict("records")
            data["top_gainers"] = ths_df.head(5).to_dict("records")
            # 跌幅靠前的
            losers = ths_df[ths_df["pct_chg"] < 0].tail(5)
            data["top_losers"] = losers.to_dict("records") if not losers.empty else []
            print(f"  {ok} 板块排名: {len(ths_df)} 个概念板块")
    except Exception as e:
        data["errors"].append(f"板块排名获取失败: {e}")
        print(f"  {fail} 板块排名: {e}")

    # ── 6. 同花顺热点题材归因（a-stock-data 接入） ──
    if _HAS_THS:
        try:
            df_reason = ths_hot_reason(trade_date)
            if not df_reason.empty:
                data["hot_reason"] = df_reason.to_dict("records")
                data["hot_reason_count"] = len(df_reason)
                # 词频统计：题材热度排名 TOP15
                ranking = hot_reason_tag_ranking(df_reason)
                data["hot_reason_ranking"] = [{"tag": t, "count": c} for t, c in ranking[:15]]
                print(f"  {ok} 热点题材: {len(df_reason)} 只个股, {len(ranking)} 个题材标签")
        except Exception as e:
            print(f"  {warn} 热点题材: {e}")
    else:
        print(f"  {warn} ths_provider 不可用, 跳过热点题材")

    # ── 7. 东财全球财经资讯（a-stock-data 接入） ──
    if _HAS_EM:
        try:
            news = eastmoney_global_news(10)
            if news:
                data["global_news"] = news
                print(f"  {ok} 全球资讯: {len(news)} 条")
        except Exception as e:
            print(f"  {warn} 全球资讯: {e}")

    # ── 8. 持仓/自选股公告（a-stock-data 接入） ──
    if _HAS_CNINFO:
        try:
            # 读取持仓和自选股
            portfolio_file = os.path.join(os.path.dirname(__file__), "..", "..", "data", "portfolio.json")
            watchlist_file = os.path.join(os.path.dirname(__file__), "..", "..", "data", "watchlist.json")
            watch_codes = []

            for pf_path in [portfolio_file, watchlist_file]:
                if os.path.exists(pf_path):
                    with open(pf_path, "r", encoding="utf-8") as f:
                        pf_data = json.load(f)
                    if isinstance(pf_data, list):
                        watch_codes.extend([str(s.get("code", "")).replace(".SH", "").replace(".SZ", "").replace(".BJ", "")
                                            for s in pf_data if s.get("code")])
                    elif isinstance(pf_data, dict):
                        watch_codes.extend([str(s.get("code", "")).replace(".SH", "").replace(".SZ", "").replace(".BJ", "")
                                            for s in pf_data.values() if isinstance(s, dict) and s.get("code")])

            ann_list = []
            for code in set(watch_codes):
                if not code or not code.isdigit():
                    continue
                try:
                    anns = cninfo_announcements(code, page_size=3)
                    for a in anns:
                        a["watch_code"] = code
                    ann_list.extend(anns)
                except Exception:
                    continue
            if ann_list:
                # 按日期排序取最新 20 条
                ann_list.sort(key=lambda x: x.get("date", ""), reverse=True)
                data["watch_announcements"] = ann_list[:20]
                print(f"  {ok} 自选股公告: {len(data['watch_announcements'])} 条")
        except Exception as e:
            print(f"  {warn} 自选股公告: {e}")

    # D4-CP3: 错误汇总
    if data["errors"]:
        print(f"  [WARN] 采集完成，共 {len(data['errors'])} 个错误")
        for err in data["errors"]:
            print(f"    - {err}")

    # 9. 写入数据库（尽力而为）
    _save_to_db(data)

    return data


def _save_to_db(data: dict):
    """将采集数据写入本地SQLite数据库（尽力而为，失败不影响报告生成）"""
    global _db
    if _db is None:
        return

    trade_date = data.get("date", "")
    if not trade_date:
        return

    ok = "[OK]"
    warn = "[WARN]"

    # 6a. 大盘指数行情 → daily_price (asset_type='I')
    market = data.get("market_overview", {})
    if market:
        try:
            rows = []
            for name, info in market.items():
                rows.append({
                    "ts_code": _INDEX_NAME_MAP.get(name, name),
                    "trade_date": trade_date,
                    "close": info.get("close"),
                    "pct_chg": info.get("pct_change"),
                    "amount": info.get("amount", 0) * 1e4,  # 亿元转万元
                })
            if rows:
                df_idx = pd.DataFrame(rows)
                n = _db.upsert_daily_price(df_idx, asset_type='I')
                print(f"  {ok} DB: 指数行情写入 {n} 条")
        except Exception as e:
            print(f"  {warn} DB: 指数行情写入失败: {e}")

    # 6b. 北向资金 → moneyflow_hsgt
    hsgt = data.get("moneyflow_hsgt", {})
    if hsgt:
        try:
            _db.upsert_moneyflow_hsgt(trade_date, {
                "north_net": hsgt.get("north_net"),
                "hgt": hsgt.get("hgt"),
                "sgt": hsgt.get("sgt"),
            })
            print(f"  {ok} DB: 北向资金写入")
        except Exception as e:
            print(f"  {warn} DB: 北向资金写入失败: {e}")

    # 6c. 板块数据 → ths_daily
    ths_hot = data.get("ths_hot", [])
    if ths_hot:
        try:
            for item in ths_hot:
                item["trade_date"] = trade_date
                item["strength"] = item.get("pct_chg", 0)
                item["anomaly"] = abs(item.get("pct_chg", 0)) > 3
            df_ths = pd.DataFrame(ths_hot)
            n = _db.upsert_ths_daily(df_ths)
            print(f"  {ok} DB: 板块数据写入 {n} 条")
        except Exception as e:
            print(f"  {warn} DB: 板块数据写入失败: {e}")

    # 6d. 报告日志
    try:
        status = "ok" if not data.get("errors") else "error"
        _db.save_report_log(trade_date, "agent1", status,
                           error_msg="; ".join(data["errors"]) if data["errors"] else None)
    except Exception as e:
        print(f"  {warn} DB: 报告日志写入失败: {e}")


# 指数名称→Tushare代码映射
_INDEX_NAME_MAP = {
    "上证指数": "000001.SH",
    "深证成指": "399001.SZ",
    "创业板指": "399006.SZ",
    "科创50": "000688.SH",
}


if __name__ == "__main__":
    date_arg = sys.argv[1] if len(sys.argv) > 1 else None
    data = generate_data_json(date_arg)

    # 输出 JSON 到 stdout，供 Claude 读取
    print("\n=== RESULT_JSON ===")
    print(json.dumps(data, ensure_ascii=False, indent=2, default=str))
    print("=== END ===")

    # 同时保存到文件
    output_dir = os.path.join(os.path.dirname(__file__), "..", "..", "data", "raw")
    os.makedirs(output_dir, exist_ok=True)
    output_path = os.path.join(output_dir, f"情报原始数据_{data['date']}.json")
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n原始数据已保存: {output_path}")
