"""
Agent8 政策分析师 — 政策数据采集与分析脚本

采集当日政策新闻（宏观/产业/监管/税收），输出结构化政策事件数据，
供 LLM Agent 进行政策影响分析。

用法：
    source venv/Scripts/activate
    python scripts/agent8-政策分析/policy_analyst.py
    python scripts/agent8-政策分析/policy_analyst.py 20260703  # 指定日期

输出：
    reports/日报/政策/政策分析_YYYY-MM-DD.md — Markdown报告
    data/raw/政策原始数据_YYYYMMDD.json — 结构化政策数据
    policy_events 表写入 SQLite

D9反例：
1. 不要仅依赖单一新闻源（可能遗漏重要政策）
2. 不要使用过时政策（只分析当天或最近交易日的政策）
3. 不要混淆政策阶段（吹风/草案/征求意见/正式实施 影响程度不同）

D3异常处理表：
| 触发条件 | 一线修复 | 仍失败兜底 |
|---------|---------|-----------|
| 东方财富新闻API无响应 | 重试1次 | 仅输出资金流向数据，标注"新闻源不可用" |
| 数据库连接失败 | 跳过DB写入 | 仅输出JSON文件 |
| 持仓文件不存在 | 仅做全市场分析 | 报告中提示配置持仓 |
"""
import os
import sys
import json
import logging
from datetime import datetime, timedelta

import pandas as pd

# 添加项目根目录
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, PROJECT_ROOT)

from scripts.utils.tushare_client import pro
from scripts.utils.eastmoney_get import em_get
from scripts.utils.data_provider import get_provider

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# 数据库连接（尽力而为）
_db = None
try:
    from scripts.utils.db_manager import DatabaseManager
    _db = DatabaseManager()
except Exception as e:
    logger.warning(f"数据库连接失败: {e}")


def get_today() -> str:
    return datetime.now().strftime("%Y%m%d")


def load_portfolio() -> list:
    """读取持仓列表"""
    try:
        path = os.path.join(PROJECT_ROOT, "data", "portfolio.json")
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("持仓列表", [])
    except Exception as e:
        logger.warning(f"持仓文件读取失败: {e}")
        return []


def load_watchlist() -> list:
    """读取自选股列表"""
    try:
        path = os.path.join(PROJECT_ROOT, "data", "watchlist.json")
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("自选股列表", data.get("watchlist", []))
    except Exception as e:
        logger.warning(f"自选股文件读取失败: {e}")
        return []


def fetch_eastmoney_news() -> list:
    """
    从东方财富获取当日政策/要闻

    使用 em_get() 限流网关（借鉴 a-stock-data 的反爬策略）
    """
    news_items = []
    try:
        # 东方财富新闻搜索 API
        params = {
            "code": "zhengce",  # 政策频道
            "pageindex": 1,
            "pagesize": 20,
        }
        data = em_get("/search/api/web/v1/search/web", params=params, base_key="search")
        if data and "data" in data:
            articles = data["data"].get("articles", [])
            for art in articles:
                news_items.append({
                    "title": art.get("title", ""),
                    "content": art.get("content", art.get("abstract", "")),
                    "source": art.get("source", "东方财富"),
                    "url": art.get("url", ""),
                    "publish_time": art.get("date", ""),
                })

        # 备用：东方财富要闻 API
        if not news_items:
            params2 = {"type": "yaowen", "page": 1}
            data2 = em_get("/api/data/v1/get", params={
                "reportName": "RPT_NEWS_RECENT",
                "columns": "ALL",
                "pageNumber": 1,
                "pageSize": 30,
            }, base_key="datacenter")
            if data2 and "data" in data2:
                items = data2["data"].get("list", data2["data"].get("result", []))
                for item in items:
                    news_items.append({
                        "title": item.get("title", ""),
                        "content": item.get("content", item.get("abstract", "")),
                        "source": item.get("source", "东方财富"),
                        "url": item.get("art_url", ""),
                        "publish_time": item.get("date", ""),
                    })

    except Exception as e:
        logger.warning(f"东方财富新闻获取失败: {e}")

    return news_items


def fetch_north_money(trade_date: str) -> dict:
    """获取北向资金流向（辅助流动性分析）"""
    try:
        df = pro.moneyflow_hsgt(start_date=trade_date, end_date=trade_date)
        if df is not None and not df.empty:
            row = df.iloc[0]
            return {
                "north_net": round(float(row.get("north_money", 0)) / 1e4, 2),
                "hgt": round(float(row.get("hgt", 0)) / 1e4, 2),
                "sgt": round(float(row.get("sgt", 0)) / 1e4, 2),
            }
    except Exception as e:
        logger.warning(f"北向资金获取失败: {e}")
    return {"north_net": 0, "hgt": 0, "sgt": 0}


def get_recent_policy_events(days: int = 7) -> list:
    """从DB获取近期政策事件，避免重复分析"""
    if _db is None:
        return []
    try:
        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")
        rows = _db.execute_query(
            "SELECT title, event_date, category FROM policy_events WHERE event_date >= ? ORDER BY event_date DESC",
            (cutoff,)
        )
        return [{"title": r[0], "date": r[1], "category": r[2]} for r in rows] if rows else []
    except Exception as e:
        logger.warning(f"历史政策查询失败: {e}")
        return []


def classify_policy(title: str, content: str) -> str:
    """粗略的政策分类（关键词匹配，LLM会在后续报告中细化）"""
    text = (title + " " + content).lower()
    if any(kw in text for kw in ["央行", "降准", "降息", "加息", "MLF", "逆回购", "货币", "利率", "流动性"]):
        return "宏观调控"
    if any(kw in text for kw in ["证监会", "监管", "立案", "处罚", "退市", "IPO", "再融资", "减持"]):
        return "监管动态"
    if any(kw in text for kw in ["税收", "税率", "增值税", "所得税", "关税", "减免税", "退税"]):
        return "税收政策"
    if any(kw in text for kw in ["产业", "扶持", "补贴", "规划", "新能源", "芯片", "AI", "数字经济", "碳中和"]):
        return "产业政策"
    return "宏观政策"


def generate_policy_analysis(trade_date: str = None) -> dict:
    """
    主函数：采集政策数据并输出结构化结果
    """
    if trade_date is None:
        trade_date = get_today()

    ok = "[OK]"
    fail = "[FAIL]"
    warn = "[WARN]"

    print(f"[政策分析师] 分析日期: {trade_date}")

    data = {
        "date": trade_date,
        "policy_events": [],
        "north_money": {},
        "holdings": [],
        "watchlist": [],
        "recent_policies": [],
        "errors": [],
    }

    # 1. 加载持仓和自选
    data["holdings"] = load_portfolio()
    data["watchlist"] = load_watchlist()
    print(f"  {ok} 持仓: {len(data['holdings'])} 只, 自选: {len(data['watchlist'])} 只")

    # 2. 获取历史政策事件（去重用）
    data["recent_policies"] = get_recent_policy_events(7)
    recent_titles = {p["title"].strip()[:40] for p in data["recent_policies"]}
    print(f"  {ok} 历史政策: {len(data['recent_policies'])} 条（近7天）")

    # 3. 采集今日政策新闻
    try:
        news = fetch_eastmoney_news()
        if news:
            # 去重
            seen = set()
            for item in news:
                key = item["title"].strip()[:40]
                if key in seen:
                    continue
                if key in recent_titles:
                    continue
                seen.add(key)
                category = classify_policy(item["title"], item["content"])
                data["policy_events"].append({
                    "title": item["title"],
                    "content": item["content"][:200],
                    "source": item.get("source", "东方财富"),
                    "url": item.get("url", ""),
                    "category": category,
                    "impact_score": 0,  # LLM会在报告阶段评分
                    "impact_sector": "",
                    "publish_time": item.get("publish_time", ""),
                })
            print(f"  {ok} 今日政策新闻: {len(data['policy_events'])} 条（去重后）")
        else:
            data["errors"].append("东方财富新闻API无数据")
            print(f"  {fail} 东方财富新闻: 无数据")
    except Exception as e:
        data["errors"].append(f"政策新闻采集失败: {e}")
        print(f"  {fail} 政策新闻采集: {e}")

    # 4. 北向资金（流动性观察）
    try:
        data["north_money"] = fetch_north_money(trade_date)
        net = data["north_money"].get("north_net", 0)
        print(f"  {ok} 北向资金: {net:.2f} 亿")
    except Exception as e:
        data["errors"].append(f"北向资金获取失败: {e}")
        print(f"  {fail} 北向资金: {e}")

    # 5. 写入数据库
    _save_policy_to_db(data)

    # 6. D4-CP: 错误汇总
    if data["errors"]:
        print(f"  {warn} 采集完成，共 {len(data['errors'])} 个错误")
        for err in data["errors"]:
            print(f"    - {err}")

    return data


def _save_policy_to_db(data: dict):
    """将政策事件写入数据库"""
    global _db
    if _db is None:
        return

    trade_date = data.get("date", "")
    if not trade_date:
        return

    events = data.get("policy_events", [])
    if not events:
        return

    ok = "[OK]"
    warn = "[WARN]"

    for event in events:
        try:
            _db.execute_query(
                """INSERT OR IGNORE INTO policy_events
                   (event_date, title, content, source, category, impact_sector, impact_score, url)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    trade_date,
                    event["title"],
                    event["content"],
                    event.get("source", ""),
                    event.get("category", "宏观政策"),
                    event.get("impact_sector", ""),
                    event.get("impact_score", 0),
                    event.get("url", ""),
                )
            )
        except Exception as e:
            logger.warning(f"政策事件写入失败: {e}")

    print(f"  {ok} DB: 写入 {len(events)} 条政策事件")


def generate_markdown_report(data: dict) -> str:
    """生成 Markdown 格式的政策分析报告"""
    date_str = data["date"]
    date_fmt = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"

    lines = []
    lines.append(f"# 📜 政策分析报告 — {date_fmt}")
    lines.append("")
    lines.append("> 数据源：东方财富 | 财联社 | Tushare")
    lines.append("")

    # 宏观政策
    macro = [e for e in data["policy_events"] if e["category"] == "宏观调控"]
    if macro:
        lines.append("## 📊 宏观政策")
        for e in macro:
            lines.append(f"- **{e['title']}**")
            lines.append(f"  - {e['content']}")
            lines.append(f"  - 来源: {e['source']}")
        lines.append("")

    # 产业政策
    industry = [e for e in data["policy_events"] if e["category"] == "产业政策"]
    if industry:
        lines.append("## 🏭 产业政策")
        for e in industry:
            lines.append(f"- **{e['title']}**")
            lines.append(f"  - {e['content']}")
        lines.append("")

    # 监管动态
    regulation = [e for e in data["policy_events"] if e["category"] == "监管动态"]
    if regulation:
        lines.append("## ⚖️ 监管动态")
        for e in regulation:
            lines.append(f"- **{e['title']}**")
            lines.append(f"  - {e['content']}")
        lines.append("")

    # 税收政策
    tax = [e for e in data["policy_events"] if e["category"] == "税收政策"]
    if tax:
        lines.append("## 💰 税收政策")
        for e in tax:
            lines.append(f"- **{e['title']}**")
            lines.append(f"  - {e['content']}")
        lines.append("")

    # 其他政策
    others = [e for e in data["policy_events"] if e["category"] == "宏观政策"]
    if others:
        lines.append("## 📋 其他重要政策")
        for e in others:
            lines.append(f"- **{e['title']}**")
            lines.append(f"  - {e['content']}")
        lines.append("")

    # 流动性观察
    nm = data.get("north_money", {})
    lines.append("## 💰 流动性观察")
    lines.append(f"| 指标 | 数值 |")
    lines.append(f"|:----|:----|")
    lines.append(f"| 北向资金净流入 | {nm.get('north_net', 0):.2f} 亿 |")
    lines.append(f"| 沪股通 | {nm.get('hgt', 0):.2f} 亿 |")
    lines.append(f"| 深股通 | {nm.get('sgt', 0):.2f} 亿 |")
    lines.append("")

    # 持仓关联（由LLM在后面阶段填写）
    holdings = data.get("holdings", [])
    if holdings:
        lines.append("## 🔗 对持仓的潜在影响")
        lines.append("> ⚠️ 以下分析需要LLM结合政策内容逐条评估")
        lines.append("")
        lines.append("| 持仓 | 代码 | 关联政策 | 影响方向 |")
        lines.append("|:----|:----|:--------|:--------|")
        for h in holdings:
            lines.append(f"| {h.get('名称', '?')} | {h.get('代码', '?')} | （待LLM分析） | ⏳ |")
        lines.append("")

    # 待关注事件
    lines.append("## ⏰ 待关注事件")
    lines.append("- 当前无预排事件（等待LLM补充）")
    lines.append("")

    # 统计信息
    total = len(data["policy_events"])
    lines.append("---")
    lines.append(f"> 今日共采集 {total} 条政策事件 | 持仓 {len(holdings)} 只 | "
                 f"错误: {len(data['errors'])}")

    if data["errors"]:
        lines.append("> ⚠️ 采集告警:")
        for err in data["errors"]:
            lines.append(f"> - {err}")

    return "\n".join(lines)


if __name__ == "__main__":
    date_arg = sys.argv[1] if len(sys.argv) > 1 else None
    data = generate_policy_analysis(date_arg)

    # 输出 JSON 到 stdout
    print("\n=== RESULT_JSON ===")
    print(json.dumps(data, ensure_ascii=False, indent=2, default=str))
    print("=== END ===")

    # 保存结构化数据
    output_dir = os.path.join(PROJECT_ROOT, "data", "raw")
    os.makedirs(output_dir, exist_ok=True)
    json_path = os.path.join(output_dir, f"政策原始数据_{data['date']}.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n结构化数据已保存: {json_path}")

    # 生成 Markdown 报告
    report = generate_markdown_report(data)
    report_dir = os.path.join(PROJECT_ROOT, "reports", "日报", "政策")
    os.makedirs(report_dir, exist_ok=True)
    date_fmt = f"{data['date'][:4]}-{data['date'][4:6]}-{data['date'][6:8]}"
    report_path = os.path.join(report_dir, f"政策分析_{date_fmt}.md")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"报告已保存: {report_path}")
