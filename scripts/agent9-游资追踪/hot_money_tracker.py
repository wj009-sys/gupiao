"""
Agent9 游资追踪师 — 龙虎榜/资金情绪/游资席位分析脚本

采集龙虎榜数据、识别知名游资席位、计算资金情绪指数、
监控持仓和自选股的资金面风险。

用法：
    source venv/Scripts/activate
    python scripts/agent9-游资追踪/hot_money_tracker.py
    python scripts/agent9-游资追踪/hot_money_tracker.py 20260703  # 指定日期

输出：
    reports/日报/游资/游资追踪_YYYY-MM-DD.md — 游资追踪报告
    data/raw/游资原始数据_YYYYMMDD.json — 结构化数据
    dragon_tiger_detail + hot_money_seats 表写入 SQLite

D9反例：
1. 不要混淆涨跌停板和龙虎榜（涨跌停板仅部分股票上龙虎榜）
2. 不要把所有大额买单都归为游资（区分机构/游资/量化）
3. 不要单日资金流向下结论（需结合3-5日趋势）

D3异常处理表：
| 触发条件 | 一线修复 | 仍失败兜底 |
|---------|---------|-----------|
| Tushare limit_list API失败 | 改用东方财富 em_get() 龙虎榜 API | 仅输出现有数据，标注龙虎榜不可用 |
| 个股资金流向获取超时 | 单个股票跳过，继续处理下一个 | 在报告中标注部分资金数据缺失 |
| 知名席位识别失败（席位数不在库中） | 按席位类型（机构/普通）默认归类 | 标注"未知席位"并补充到席位库 |
| 数据库写入失败 | 打印警告，继续生成报告 | 数据仅保存到JSON文件 |
| 涨停板池API失败 | 捕获异常，跳过打板情绪 | 只显示龙虎榜部分 |
| 同花顺热榜API失败 | 捕获异常，跳过热榜 | 只显示其他数据 |
"""
import os
import sys
import json
import logging
from datetime import datetime, timedelta

import pandas as pd

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, PROJECT_ROOT)

from scripts.utils.tushare_client import pro
from scripts.utils.eastmoney_get import em_get, get_dragon_tiger, get_moneyflow_stock

# ── a-stock-data 新数据源（优雅降级） ──
try:
    from scripts.utils.limit_up_board import (
        em_zt_pool, em_zb_pool, em_dt_pool,
        limit_up_sentiment, sentiment_summary_text
    )
    _HAS_LIMIT_UP = True
except Exception:
    _HAS_LIMIT_UP = False
try:
    from scripts.utils.ths_provider import ths_hot_list
    _HAS_THS = True
except Exception:
    _HAS_THS = False

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

_db = None
try:
    from scripts.utils.db_manager import DatabaseManager
    _db = DatabaseManager()
except Exception as e:
    logger.warning(f"数据库连接失败: {e}")

# ── 知名游资席位库（持续更新） ──
# 格式: {座位名称: {"style": "打板/趋势/低吸", "type": "游资/机构/量化"}}
WELL_KNOWN_SEATS = {
    # 顶级游资
    "中信证券股份有限公司上海分公司": {"style": "打板", "type": "游资"},
    "中信证券股份有限公司上海溧阳路证券营业部": {"style": "打板", "type": "游资"},
    "国泰君安证券股份有限公司上海江苏路证券营业部": {"style": "打板", "type": "游资"},
    "华泰证券股份有限公司深圳益田路荣超商务中心证券营业部": {"style": "打板", "type": "游资"},
    "中国银河证券股份有限公司绍兴证券营业部": {"style": "打板", "type": "游资"},
    "中信证券股份有限公司杭州延安路证券营业部": {"style": "趋势", "type": "游资"},
    "中信证券股份有限公司上海淮海中路证券营业部": {"style": "趋势", "type": "游资"},
    # 新生代游资
    "东方财富证券股份有限公司拉萨团结路第二证券营业部": {"style": "打板", "type": "游资"},
    "东方财富证券股份有限公司拉萨东环路第二证券营业部": {"style": "打板", "type": "游资"},
    "东方财富证券股份有限公司拉萨团结路第一证券营业部": {"style": "打板", "type": "游资"},
    "东方财富证券股份有限公司拉萨东环路第一证券营业部": {"style": "打板", "type": "游资"},
    # 量化席位
    "中国国际金融股份有限公司上海分公司": {"style": "趋势", "type": "量化"},
    "中国国际金融股份有限公司北京建国门外大街证券营业部": {"style": "趋势", "type": "量化"},
    "华泰证券股份有限公司总部": {"style": "趋势", "type": "量化"},
    # 机构专用
    "机构专用": {"style": "趋势", "type": "机构"},
    "深股通专用": {"style": "趋势", "type": "外资"},
    "沪股通专用": {"style": "趋势", "type": "外资"},
}


def get_today() -> str:
    return datetime.now().strftime("%Y%m%d")


def load_portfolio() -> list:
    try:
        path = os.path.join(PROJECT_ROOT, "data", "portfolio.json")
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("持仓列表", [])
    except Exception as e:
        logger.warning(f"持仓文件读取失败: {e}")
        return []


def fetch_limit_list_tushare(trade_date: str) -> pd.DataFrame:
    """
    从 Tushare 获取龙虎榜（涨跌停板）

    优先用 pro.limit_list()，失败后尝试 pro.top_list()
    """
    try:
        df = pro.limit_list(trade_date=trade_date)
        if df is not None and not df.empty:
            cols = [c for c in ["ts_code", "name", "close", "pct_chg", "amount",
                                 "buy_amount", "sell_amount", "net_amount"]
                    if c in df.columns]
            result = df[cols].copy() if cols else df.copy()
            # 补充净买入
            if "net_amount" not in result.columns and "buy_amount" in result.columns and "sell_amount" in result.columns:
                result["net_amount"] = result["buy_amount"] - result["sell_amount"]
            return result
    except Exception as e:
        logger.warning(f"Tushare limit_list 失败: {e}")

    # 尝试 top_list (更完整的龙虎榜)
    try:
        df = pro.top_list(trade_date=trade_date)
        if df is not None and not df.empty:
            return df
    except Exception as e:
        logger.warning(f"Tushare top_list 失败: {e}")

    return pd.DataFrame()


def fetch_dragon_tiger_eastmoney(trade_date: str) -> list:
    """
    从东方财富获取龙虎榜数据（作为 Tushare 的备用）

    使用 em_get() 限流网关
    """
    try:
        data = get_dragon_tiger(trade_date)
        if data and "data" in data:
            items = data["data"].get("list", data["data"].get("result", []))
            result = []
            for item in items:
                result.append({
                    "ts_code": item.get("SECURITY_CODE", "") + ".SH" if item.get("SECURITY_CODE", "").startswith("6") else item.get("SECURITY_CODE", "") + ".SZ",
                    "name": item.get("SECURITY_NAME", ""),
                    "close": item.get("CLOSE_PRICE", 0),
                    "pct_chg": item.get("CHANGE_RATE", 0),
                    "amount": item.get("TOTAL_AMOUNT", 0),
                    "buy_amount": item.get("BUY_AMOUNT", 0),
                    "sell_amount": item.get("SELL_AMOUNT", 0),
                    "net_amount": item.get("NET_BUY_AMT", 0),
                    "reason_type": item.get("BILLBOARD_TYPE_NAME", ""),
                })
            return result
    except Exception as e:
        logger.warning(f"东方财富龙虎榜获取失败: {e}")

    return []


def identify_hot_money_seats(buy_seats_text: str, sell_seats_text: str) -> list:
    """
    识别文本中的游资席位

    Args:
        buy_seats_text: 买入席位文本，如 "中信上海分公司(500万),机构专用(300万)"
        sell_seats_text: 卖出席位文本

    Returns:
        [{seat_name, style, type, net_amount}]
    """
    seats = []
    all_text = (buy_seats_text or "") + "," + (sell_seats_text or "")

    for seat_name, info in WELL_KNOWN_SEATS.items():
        if seat_name in all_text:
            seats.append({
                "seat_name": seat_name,
                "style": info["style"],
                "type": info["type"],
            })

    return seats


def calculate_sentiment_index(limit_df: pd.DataFrame) -> dict:
    """
    计算资金情绪指数

    基于：涨停家数/跌停家数/连板高度/净买入总额
    """
    if limit_df.empty:
        return {"rating": "数据不足", "score": 0}

    try:
        up_count = int((limit_df["pct_chg"] > 0).sum()) if "pct_chg" in limit_df.columns else 0
        down_count = int((limit_df["pct_chg"] < 0).sum()) if "pct_chg" in limit_df.columns else 0
        total_net = float(limit_df["net_amount"].sum()) / 1e4 if "net_amount" in limit_df.columns else 0

        # 情绪评分（0-100）
        score = 50  # 中性基线

        # 涨跌比
        total = up_count + down_count
        if total > 0:
            ratio = up_count / total
            score += (ratio - 0.5) * 60  # ±30

        # 净买入
        score += min(max(total_net / 5, -10), 10)  # ±10

        # 连板加分（如果有连板数据）
        score = min(max(score, 0), 100)

        if score >= 70:
            rating = "亢奋"
        elif score >= 55:
            rating = "活跃"
        elif score >= 40:
            rating = "低迷"
        else:
            rating = "冰点"

        return {
            "score": round(score, 1),
            "rating": rating,
            "up_count": up_count,
            "down_count": down_count,
            "total_net": round(total_net, 2),
        }
    except Exception as e:
        logger.warning(f"情绪指数计算失败: {e}")
        return {"rating": "计算失败", "score": 0}


def generate_hot_money_tracking(trade_date: str = None) -> dict:
    """
    主函数：采集游资数据并输出结构化结果
    """
    if trade_date is None:
        trade_date = get_today()

    ok = "[OK]"
    fail = "[FAIL]"
    warn = "[WARN]"

    print(f"[游资追踪师] 分析日期: {trade_date}")

    data = {
        "date": trade_date,
        "dragon_tiger": [],
        "hot_money_seats": {},
        "sentiment": {},
        "holdings_moneyflow": [],
        "holdings": [],
        # a-stock-data 新数据
        "limit_up_sentiment": {},      # 涨停打板情绪
        "limit_up_summary_text": "",   # 情绪文字摘要
        "ths_hot_list": [],            # 同花顺热榜
        "errors": [],
    }

    # 1. 加载持仓
    data["holdings"] = load_portfolio()
    print(f"  {ok} 持仓: {len(data['holdings'])} 只")

    # 2. 获取龙虎榜数据（Tushare 优先）
    try:
        lb_df = fetch_limit_list_tushare(trade_date)
        if not lb_df.empty:
            records = lb_df.head(20).to_dict("records")
            data["dragon_tiger"] = records
            print(f"  {ok} 龙虎榜(Tushare): {len(records)} 条")
        else:
            # 备用：东方财富
            em_records = fetch_dragon_tiger_eastmoney(trade_date)
            if em_records:
                data["dragon_tiger"] = em_records
                print(f"  {ok} 龙虎榜(东方财富): {len(em_records)} 条")
            else:
                data["errors"].append("龙虎榜数据不可用（两个数据源均无数据）")
                print(f"  {fail} 龙虎榜: 双源均不可用")
    except Exception as e:
        data["errors"].append(f"龙虎榜获取失败: {e}")
        print(f"  {fail} 龙虎榜: {e}")

    # 3. 识别游资席位
    try:
        # 从龙虎榜数据中提取席位信息（Tushare top_list 才有席位明细）
        all_seats = {}
        for item in data["dragon_tiger"]:
            buy_seats_text = item.get("buy_seats", "") or str(item.get("buy_reason", ""))
            sell_seats_text = item.get("sell_seats", "") or ""
            detected = identify_hot_money_seats(buy_seats_text, sell_seats_text)
            for seat in detected:
                name = seat["seat_name"]
                if name not in all_seats:
                    all_seats[name] = {"seat_name": name, "style": seat["style"],
                                       "type": seat["type"], "count": 0}
                all_seats[name]["count"] += 1

        data["hot_money_seats"] = list(all_seats.values())
        if data["hot_money_seats"]:
            print(f"  {ok} 游资席位: 识别到 {len(data['hot_money_seats'])} 个活跃席位")
        else:
            # 如果 top_list 没有席位文本，用简易方式统计
            print(f"  {ok} 游资席位: 当前数据源无席位明细，等待LLM补充分析")
    except Exception as e:
        logger.warning(f"游资席位识别失败: {e}")

    # 4. 计算情绪指数
    try:
        if data["dragon_tiger"]:
            sentiment_df = pd.DataFrame(data["dragon_tiger"])
            data["sentiment"] = calculate_sentiment_index(sentiment_df)
            s = data["sentiment"]
            print(f"  {ok} 情绪指数: {s.get('rating', '?')}({s.get('score', 0)}) "
                  f"涨停{s.get('up_count',0)}/跌停{s.get('down_count',0)}")
        else:
            data["sentiment"] = {"rating": "无数据", "score": 0}
            print(f"  {warn_label()} 情绪指数: 无龙虎榜数据")
    except Exception as e:
        data["errors"].append(f"情绪指数计算失败: {e}")
        data["sentiment"] = {"rating": "计算失败", "score": 0}

    # 5. 打板情绪数据（a-stock-data 接入）
    if _HAS_LIMIT_UP:
        try:
            # 尝试最近的交易日
            from datetime import date as _d
            for offset in range(7):
                ds = (_d.today() - timedelta(days=offset)).strftime("%Y%m%d")
                zt_test = em_zt_pool(ds)
                if zt_test:
                    data["limit_up_sentiment"] = limit_up_sentiment(ds)
                    data["limit_up_summary_text"] = sentiment_summary_text(ds)
                    data["limit_up_date"] = ds
                    data["limit_up_zt_count"] = len(zt_test)
                    data["limit_up_zb_count"] = len(em_zb_pool(ds))
                    data["limit_up_dt_count"] = len(em_dt_pool(ds))
                    print(f"  {ok} 打板情绪: {data['limit_up_zt_count']}涨停 "
                          f"{data['limit_up_zb_count']}炸板 {data['limit_up_dt_count']}跌停")
                    break
        except Exception as e:
            logger.warning(f"打板情绪获取失败: {e}")

    # 6. 同花顺热榜（a-stock-data 接入）
    if _HAS_THS:
        try:
            hot_list = ths_hot_list("day")
            if hot_list:
                data["ths_hot_list"] = hot_list[:20]  # TOP 20
                print(f"  {ok} 同花顺热榜: {len(data['ths_hot_list'])} 只")
        except Exception as e:
            logger.warning(f"热榜获取失败: {e}")

    # 7. 持仓资金流向（用 Tushare moneyflow）
    for h in data["holdings"]:
        code = h.get("代码", "")
        if not code:
            continue
        try:
            mf = pro.moneyflow(ts_code=code, start_date=trade_date, end_date=trade_date)
            if mf is not None and not mf.empty:
                row = mf.iloc[0]
                data["holdings_moneyflow"].append({
                    "ts_code": code,
                    "name": h.get("名称", ""),
                    "net_amount": float(row.get("buy_lg_amount", 0) - row.get("sell_lg_amount", 0)) / 1e4,
                    "buy_lg_amount": float(row.get("buy_lg_amount", 0)) / 1e4 if "buy_lg_amount" in row else 0,
                    "sell_lg_amount": float(row.get("sell_lg_amount", 0)) / 1e4 if "sell_lg_amount" in row else 0,
                    "buy_sm_amount": float(row.get("buy_sm_amount", 0)) / 1e4 if "buy_sm_amount" in row else 0,
                    "sell_sm_amount": float(row.get("sell_sm_amount", 0)) / 1e4 if "sell_sm_amount" in row else 0,
                })
        except Exception as e:
            logger.warning(f"个股资金流向获取失败 ({code} {h.get('名称', '')}): {e}")
            continue

    print(f"  {ok if data['holdings_moneyflow'] else warn} 持仓资金流向: {len(data['holdings_moneyflow'])} 只")

    # 6. 写入数据库
    _save_to_db(data)

    # 错误汇总
    if data["errors"]:
        print(f"  [WARN] 采集完成，共 {len(data['errors'])} 个错误")
        for err in data["errors"]:
            print(f"    - {err}")

    return data


def warn_label():
    return "[WARN]"


def _save_to_db(data: dict):
    """写入龙虎榜和游资数据到数据库"""
    global _db
    if _db is None:
        return

    trade_date = data.get("date", "")
    ok = "[OK]"

    # 写入龙虎榜明细
    for item in data.get("dragon_tiger", []):
        try:
            _db.upsert_dragon_tiger_detail({
                "trade_date": trade_date,
                "ts_code": item.get("ts_code", ""),
                "name": item.get("name", ""),
                "close": item.get("close", 0),
                "pct_chg": item.get("pct_chg", 0),
                "amount": item.get("amount", 0),
                "buy_amount": item.get("buy_amount", 0),
                "sell_amount": item.get("sell_amount", 0),
                "net_amount": item.get("net_amount", 0),
                "buy_seats": item.get("buy_seats", []),
                "sell_seats": item.get("sell_seats", []),
                "reason_type": item.get("reason_type", ""),
            })
        except Exception as e:
            logger.warning(f"龙虎榜写入失败: {e}")

    # 写入游资席位
    for seat in data.get("hot_money_seats", []):
        try:
            _db.upsert_hot_money_seats({
                "trade_date": trade_date,
                "seat_name": seat.get("seat_name", ""),
                "seat_type": seat.get("type", ""),
                "style": seat.get("style", ""),
                "active_stocks": seat.get("count", 0),
            })
        except Exception as e:
            logger.warning(f"游资席位写入失败: {e}")

    # 写入个股资金流向（批量化：构建DataFrame调用已有API）
    mf_list = data.get("holdings_moneyflow", [])
    if mf_list:
        try:
            import pandas as pd
            mf_rows = []
            for mf in mf_list:
                mf_rows.append({
                    "ts_code": mf.get("ts_code", ""),
                    "trade_date": trade_date,
                    "net_amount": mf.get("net_amount", 0),
                    "buy_lg_amount": mf.get("buy_lg_amount", 0),
                    "sell_lg_amount": mf.get("sell_lg_amount", 0),
                    "buy_sm_amount": mf.get("buy_sm_amount", 0),
                    "sell_sm_amount": mf.get("sell_sm_amount", 0),
                    "net_lg_amount": mf.get("buy_lg_amount", 0) - mf.get("sell_lg_amount", 0),
                })
            if mf_rows:
                _db.upsert_moneyflow_stock(pd.DataFrame(mf_rows))
        except Exception as e:
            logger.warning(f"资金流向写入失败: {e}")

    if data.get("dragon_tiger") or data.get("hot_money_seats"):
        print(f"  {ok} DB: 游资数据写入完成")


def generate_markdown_report(data: dict) -> str:
    """生成 Markdown 格式的游资追踪报告"""
    date_str = data["date"]
    date_fmt = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"

    lines = []
    lines.append(f"# 🔥 游资追踪报告 — {date_fmt}")
    lines.append("")

    # 龙虎榜概况
    dt = data.get("dragon_tiger", [])
    lines.append("## 📋 龙虎榜概况")
    if dt:
        lines.append("| 股票 | 涨跌幅% | 净买入(万) | 上榜类型 |")
        lines.append("|:----|:-------|:----------|:--------|")
        for item in dt[:10]:
            net = item.get("net_amount", 0) / 1e4 if abs(item.get("net_amount", 0)) > 1e4 else item.get("net_amount", 0)
            lines.append(
                f"| {item.get('name', '?')}({item.get('ts_code', '')[:6]}) "
                f"| {item.get('pct_chg', 0):.2f} "
                f"| {net:.0f} "
                f"| {item.get('reason_type', '—')} |"
            )
    else:
        lines.append("> 龙虎榜数据暂不可用")
    lines.append("")

    # 知名游资动向
    seats = data.get("hot_money_seats", [])
    lines.append("## 🏛️ 知名游资动向")
    if seats:
        lines.append("| 席位 | 类型 | 风格 | 今日操作笔数 |")
        lines.append("|:----|:----|:----|:-----------|")
        for seat in seats:
            lines.append(
                f"| {seat.get('seat_name', '?')} "
                f"| {seat.get('type', '?')} "
                f"| {seat.get('style', '?')} "
                f"| {seat.get('count', 0)} |"
            )
    else:
        lines.append("> 当前数据源无席位明细，需LLM在报告阶段补充分析")
    lines.append("")

    # 情绪指标
    sentiment = data.get("sentiment", {})
    lines.append("## 📊 资金情绪指标")
    lines.append(f"| 指标 | 数值 |")
    lines.append(f"|:----|:----|")
    lines.append(f"| 情绪评分 | {sentiment.get('score', '—')} |")
    emoji_map = {"亢奋": "😄", "活跃": "🙂", "低迷": "😶", "冰点": "🥶"}
    emoji = emoji_map.get(sentiment.get("rating", ""), "—")
    lines.append(f"| 情绪评级 | {emoji} {sentiment.get('rating', '—')} |")
    lines.append(f"| 龙虎榜涨停 | {sentiment.get('up_count', '—')} |")
    lines.append(f"| 龙虎榜跌停 | {sentiment.get('down_count', '—')} |")
    lines.append(f"| 净买入总额 | {sentiment.get('total_net', '—')} 亿 |")
    lines.append("")

    # 打板情绪（a-stock-data 接入）
    limit_up_text = data.get("limit_up_summary_text", "")
    if limit_up_text:
        lines.append("## 🚀 涨停打板情绪")
        for l in limit_up_text.split("\n"):
            lines.append(l)
        lines.append("")

    # 同花顺热榜（a-stock-data 接入）
    hot_list = data.get("ths_hot_list", [])
    if hot_list:
        lines.append("## 🔥 市场人气热榜 TOP10")
        lines.append("| 排名 | 股票 | 热度 | 涨跌幅% | 概念标签 |")
        lines.append("|:----|:----|:----|:-------|:--------|")
        for s in hot_list[:10]:
            concepts = ", ".join((s.get("concepts") or [])[:3])
            lines.append(
                f"| #{s.get('rank', '?')} "
                f"| {s.get('name', '?')} "
                f"| {s.get('heat', '—')} "
                f"| {s.get('pct', '—')} "
                f"| {concepts} |"
            )
        lines.append("")

    # 持仓资金面监控
    mf = data.get("holdings_moneyflow", [])
    lines.append("## 🔍 持仓资金面监控")
    if mf:
        lines.append("| 持仓 | 主力净额(万) | 大单买入(万) | 大单卖出(万) | 提示 |")
        lines.append("|:----|:-----------|:-----------|:-----------|:----|")
        for item in mf:
            net = item.get("net_amount", 0)
            buy_lg = item.get("buy_lg_amount", 0)
            sell_lg = item.get("sell_lg_amount", 0)
            warning = "🟢 正常" if net > -100 else ("⚠️ 注意" if net > -500 else "🔴 风险")
            lines.append(
                f"| {item.get('name', '?')} "
                f"| {net:.0f} "
                f"| {buy_lg:.0f} "
                f"| {sell_lg:.0f} "
                f"| {warning} |"
            )
    else:
        lines.append("> 持仓资金流向数据暂不可用")
    lines.append("")

    # 游资关注方向
    lines.append("## 💡 游资关注方向（等待LLM补充分析）")
    lines.append("")
    lines.append("> 基于龙虎榜数据和情绪指标，LLM需在报告阶段补充：")
    lines.append("> 1. 游资集中关注的板块")
    lines.append("> 2. 风格偏好（主板/创业板/科创板）")
    lines.append("> 3. 对当日交易策略的参考建议")
    lines.append("")

    # 统计信息
    lines.append("---")
    lines.append(f"> 龙虎榜: {len(dt)} 条 | 游资席位: {len(seats)} 个 | "
                 f"持仓资金: {len(mf)} 只 | 错误: {len(data['errors'])}")
    if data["errors"]:
        lines.append("> ⚠️ 采集告警:")
        for err in data["errors"]:
            lines.append(f"> - {err}")

    return "\n".join(lines)


if __name__ == "__main__":
    date_arg = sys.argv[1] if len(sys.argv) > 1 else None
    data = generate_hot_money_tracking(date_arg)

    # 输出 JSON
    print("\n=== RESULT_JSON ===")
    print(json.dumps(data, ensure_ascii=False, indent=2, default=str))
    print("=== END ===")

    # 保存结构化数据
    output_dir = os.path.join(PROJECT_ROOT, "data", "raw")
    os.makedirs(output_dir, exist_ok=True)
    json_path = os.path.join(output_dir, f"游资原始数据_{data['date']}.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n结构化数据已保存: {json_path}")

    # 生成 Markdown 报告
    report = generate_markdown_report(data)
    report_dir = os.path.join(PROJECT_ROOT, "reports", "日报", "游资")
    os.makedirs(report_dir, exist_ok=True)
    date_fmt = f"{data['date'][:4]}-{data['date'][4:6]}-{data['date'][6:8]}"
    report_path = os.path.join(report_dir, f"游资追踪_{date_fmt}.md")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"报告已保存: {report_path}")
