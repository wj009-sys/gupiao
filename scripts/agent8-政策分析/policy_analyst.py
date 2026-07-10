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
    """读取自选股列表（支持中文分类key格式）"""
    try:
        path = os.path.join(PROJECT_ROOT, "data", "watchlist.json")
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        # 中文分类key格式: {"环保与新能源": ["600388.SH"], "有色金属": [...]}
        codes = []
        for key, val in data.items():
            if isinstance(val, list) and key != "说明":
                for item in val:
                    if isinstance(item, str) and "." in item:
                        codes.append(item)
        return codes if codes else data.get("watchlist", [])
    except Exception as e:
        logger.warning(f"自选股文件读取失败: {e}")
        return []


def fetch_eastmoney_news() -> list:
    """
    从东方财富获取当日政策/要闻

    D3: 东财API无响应 → 返回空交给CLS fallback
    """
    news_items = []
    try:
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


def fetch_web_news(max_items: int = 20) -> list:
    """
    从证券时报等金融网站抓取当日要闻（东财API不可用时的备选）

    抓取来源:
        1. 证券时报 stcn.com（可靠，反爬弱）
        2. 东方财富 finance.eastmoney.com（页面抓取）

    D3: 所有来源均失败 → 返回空列表
    """
    news_items = []
    seen_titles = set()

    from scripts.utils._proxy import get_session_with_proxy
    import re

    session = get_session_with_proxy()
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "Chrome/120.0.0.0 Safari/537.36"
    }

    sources = [
        # 证券时报首页（最可靠）
        ("证券时报", "https://www.stcn.com/",
         r'<a[^>]*href="/article/detail/\d+\.html"[^>]*>(.*?)</a>'),
        # 东方财富财经要闻
        ("东方财富", "https://finance.eastmoney.com/a/czqyw.html",
         r'<a[^>]*title="([^"]+)"[^>]*href="https?://finance\.eastmoney\.com/a/'),
    ]

    for source_name, url, pattern in sources:
        if len(news_items) >= max_items:
            break
        try:
            resp = session.get(url, timeout=15, headers=headers)
            if resp.status_code != 200:
                continue

            html = resp.text
            found = re.findall(pattern, html, re.DOTALL)
            for raw_title in found:
                # 移除内部HTML标签
                title = re.sub(r'<[^>]+>', '', raw_title).strip()
                # 清洗HTML实体和多余空格
                title = title.replace("&nbsp;", " ").replace("&amp;", "&")
                title = re.sub(r'\s+', ' ', title)
                if title and len(title) > 6 and title not in seen_titles:
                    seen_titles.add(title)
                    news_items.append({
                        "title": title,
                        "content": "",
                        "source": source_name,
                        "url": "",
                        "publish_time": "",
                    })
                    if len(news_items) >= max_items:
                        break

            if news_items:
                logger.info(f"{source_name}: 获取 {len(news_items)} 条新闻")
        except Exception as e:
            logger.warning(f"{source_name} 抓取失败: {e}")
            continue

    return news_items[:max_items]


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
        rows = _db.get_policy_events(since_date=cutoff)
        return [{"title": r["title"], "date": r["event_date"], "category": r["category"]} for r in rows] if rows else []
    except Exception as e:
        logger.warning(f"历史政策查询失败: {e}")
        return []


def analyze_holdings_impact(policy_events: list, holdings: list) -> list:
    """
    用LLM分析政策对持仓的影响

    输入当前日政策事件和持仓列表，让LLM匹配政策→持仓关联，
    输出结构化影响分析（每只持仓受影响政策+影响方向+理由）。

    D3: LLM不可用/调用失败 → 规则匹配降级 → 仍失败则返回空分析
    """
    if not policy_events or not holdings:
        return []

    # 尝试LLM分析（分批，每批最多8只降低JSON复杂度）
    batch_size = 8
    all_results = []
    for batch_start in range(0, len(holdings), batch_size):
        batch = holdings[batch_start:batch_start + batch_size]
        result = _llm_holdings_analysis(policy_events, batch)
        if result:
            all_results.extend(result)
            logger.info(f"[政策] LLM分析批次 {batch_start//batch_size + 1}: {len(result)} 只")
        else:
            # 某批失败，用规则补充
            fallback = _rule_holdings_analysis(policy_events, batch)
            all_results.extend(fallback)
            logger.info(f"[政策] 规则补充批次 {batch_start//batch_size + 1}: {len(fallback)} 只")
        import time as _t
        _t.sleep(0.5)  # 批次间隔避免限流

    if all_results:
        return all_results

    # 全量降级：规则匹配
    logger.info("[政策] LLM全失败，降级到规则匹配")
    return _rule_holdings_analysis(policy_events, holdings)


def _extract_json(text: str):
    """健壮地从文本中提取JSON对象/数组"""
    import re as _re
    if not text:
        return None

    # 提取 ```json ... ``` 代码块
    code_match = _re.search(r'```(?:json)?\s*([\s\S]*?)```', text)
    if code_match:
        text = code_match.group(1).strip()

    # 定位JSON起始
    start = text.find('[')
    if start == -1:
        start = text.find('{')
    if start < 0:
        return None

    text = text[start:]
    # 找最可能的结束位置
    depth = 0
    in_str = False
    escape = False
    end = -1
    for i, ch in enumerate(text):
        if escape:
            escape = False
            continue
        if ch == '\\':
            escape = True
            continue
        if ch == '"' and not escape:
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch in ('[', '{'):
            depth += 1
        elif ch in (']', '}'):
            depth -= 1
            if depth == 0:
                end = i + 1
                break

    if end > 0:
        json_str = text[:end]
    else:
        json_str = text

    # 尝试解析
    try:
        return json.loads(json_str)
    except json.JSONDecodeError:
        pass

    # 如果主JSON解析失败，尝试找闭合结构
    try:
        # 尝试找 { } 包裹的对象
        obj_start = text.find('{')
        if obj_start >= 0:
            obj_text = text[obj_start:]
            # 简化：直接用 json.loads 尝试
            for end_char in ('}', ']'):
                idx = obj_text.rfind(end_char)
                if idx > 0:
                    try:
                        return json.loads(obj_text[:idx + 1])
                    except json.JSONDecodeError:
                        continue
    except Exception:
        pass

    return None


def _llm_holdings_analysis(policy_events: list, holdings: list) -> list:
    """用LLM分析持仓影响"""
    try:
        sys.path.insert(0, PROJECT_ROOT)
        from scripts.utils.l2_rerank import get_llm_config, is_llm_available
        if not is_llm_available():
            return []
        cfg = get_llm_config()
        from litellm import completion
    except ImportError:
        logger.warning("[政策] litellm未安装")
        return []
    except Exception:
        return []

    # 构建持仓摘要
    holding_lines = []
    for h in holdings:
        holding_lines.append(f"- {h.get('代码','?')} {h.get('名称','?')} "
                             f"[{h.get('类型','股票')}] 仓位{h.get('仓位比例',0):.1f}%")
    holdings_text = "\n".join(holding_lines)

    # 构建政策摘要（转义特殊字符防JSON破坏）
    policy_lines = []
    for i, p in enumerate(policy_events, 1):
        title = p["title"].replace('"', "'").replace('"', "'").replace("\n", " ")
        policy_lines.append(f"  [{i}] [{p['category']}] {title}")
    policies_text = "\n".join(policy_lines)

    prompt = f"""今天是{datetime.now().strftime('%Y-%m-%d')}。

## 今日政策事件（{len(policy_events)}条）
{policies_text}

## 持仓组合（{len(holdings)}只）
{holdings_text}

请逐只分析上述每只持仓受哪些政策事件影响，输出JSON数组：
[
  {{
    "code": "600388.SH",
    "name": "龙净环保",
    "impact": "利好/利空/中性",        // 整体影响方向
    "confidence": "高/中/低",           // 信心程度
    "affected_by": ["相关政策标题..."],  // 关联的具体政策
    "reason": "简要分析（20字内）"
  }}
]"""

    try:
        model_name = f"{cfg['provider']}/{cfg['model']}" if "/" not in cfg["model"] else cfg["model"]
        kwargs = {}
        if cfg.get("api_base"): kwargs["api_base"] = cfg["api_base"]
        if cfg.get("api_key"): kwargs["api_key"] = cfg["api_key"]

        response = completion(
            model=model_name,
            messages=[
                {"role": "system", "content": "你是A股政策分析师。逐只分析持仓受今日政策影响，输出JSON。"},
                {"role": "user", "content": prompt},
            ],
            response_format={"type": "json_object"},
            max_tokens=2048,
            temperature=0.1,
            timeout=30,
            **kwargs,
        )

        content = response.choices[0].message.content
        if not content:
            return []

        # 容错JSON提取：正则逐条匹配而非全量解析
        # DeepSeek有时在长列表JSON末尾产生格式问题，逐条解析更稳
        import re as _re
        # 替换破坏JSON的中文标点
        clean = content.replace('“', '"').replace('”', '"')
        clean = clean.replace("'", '"')
        # 提取每条 { ... } 记录
        items = _re.findall(
            r'\{\s*"code"\s*:\s*"([^"]+)"\s*,'
            r'\s*"name"\s*:\s*"([^"]*)"\s*,'
            r'\s*"impact"\s*:\s*"([^"]+)"\s*,'
            r'\s*"confidence"\s*:\s*"([^"]*)"\s*,'
            r'\s*"affected_by"\s*:\s*\[(.*?)\]\s*,?'
            r'\s*"reason"\s*:\s*"([^"]*)"\s*\}',
            clean, _re.DOTALL
        )
        result = []
        for code, name, impact, confidence, affected_raw, reason in items:
            affected = [a.strip().strip('"') for a in affected_raw.split(',') if a.strip()]
            result.append({
                "code": code.strip(),
                "name": name.strip(),
                "impact": impact.strip(),
                "confidence": confidence.strip() or "中",
                "affected_by": affected,
                "reason": reason.strip(),
            })
        if result:
            logger.info(f"[政策] LLM持仓分析(正则提取): {len(result)} 只")
            return result

        # 正则提取失败，回退到标准JSON解析
        try:
            parsed = json.loads(clean)
        except json.JSONDecodeError:
            # 截断到最近的闭合括号
            for closer in (']', '}'):
                idx = clean.rfind(closer)
                if idx > 0:
                    try:
                        parsed = json.loads(clean[:idx+1])
                        break
                    except json.JSONDecodeError:
                        continue
            else:
                logger.warning(f"[政策] LLM JSON完全解析失败")
                return []

        if isinstance(parsed, list):
            result = parsed
        elif isinstance(parsed, dict):
            result = (parsed.get("holdings") or parsed.get("analysis")
                      or parsed.get("results") or parsed.get("data")
                      or next((v for v in parsed.values() if isinstance(v, list)), []))
        else:
            return []
        if isinstance(parsed, list):
            result = parsed
        elif isinstance(parsed, dict):
            # json_object模式可能包装在对象里
            result = (parsed.get("holdings") or parsed.get("analysis")
                      or parsed.get("results") or parsed.get("data")
                      or next((v for v in parsed.values() if isinstance(v, list)), []))
        else:
            return []

        # 验证格式
        valid = []
        for item in result:
            if item.get("code") and item.get("impact"):
                valid.append(item)
        if valid:
            logger.info(f"[政策] LLM持仓分析: {len(valid)} 只")
            return valid
        return []

    except Exception as e:
        logger.warning(f"[政策] LLM分析失败: {e}")
        return []


# ---------- 规则匹配降级 ----------

# 行业/主题 → 持仓代码映射（自动从持仓构建）
_SECTOR_HOLDING_MAP = None

def _build_sector_map(holdings: list) -> dict:
    """构建行业→持仓的映射表"""
    mapping = {
        "环保": [], "新能源": [], "碳中和": [], "光伏": [], "风电": [],
        "芯片": [], "半导体": [], "AI": [], "人工智能": [], "数字经济": [],
        "消费": [], "白酒": [], "食品": [],
        "医药": [], "医疗": [], "创新药": [],
        "银行": [], "券商": [], "保险": [], "金融": [],
        "地产": [], "基建": [], "建材": [],
        "有色": [], "黄金": [], "稀土": [],
        "汽车": [], "新能源车": [], "锂电池": [],
        "通信": [], "5G": [], "算力": [],
        "军工": [], "航天": [],
        "煤炭": [], "钢铁": [], "电力": [],
        "国债": [], "债券": [], "利率": [], "货币": [],
    }
    # 从持仓名称提取关键词匹配
    name_map = {h.get("代码", ""): h.get("名称", "") for h in holdings}
    for sector in mapping:
        for code, name in name_map.items():
            if any(kw in name for kw in [sector]):
                mapping[sector].append(code)
    return mapping


def _rule_holdings_analysis(policy_events: list, holdings: list) -> list:
    """规则降级：关键词匹配政策→持仓"""
    global _SECTOR_HOLDING_MAP
    if _SECTOR_HOLDING_MAP is None:
        _SECTOR_HOLDING_MAP = _build_sector_map(holdings)

    result = []
    for h in holdings:
        code = h.get("代码", "")
        name = h.get("名称", "")
        affected = []
        directions = set()

        for p in policy_events:
            title = p.get("title", "")
            category = p.get("category", "")
            matched = False

            # 检查政策分类是否匹配
            if category == "宏观调控" and any(kw in title for kw in ["利率", "货币", "降准", "降息", "流动性"]):
                for bond_kw in ["国债", "债券", "利率"]:
                    if bond_kw in name:
                        affected.append(p["title"])
                        directions.add("利好" if any(kw in title for kw in ["降准", "降息", "宽松"]) else "中性")
                        matched = True
                        break
                if not matched:
                    affected.append(p["title"])
                    directions.add("中性")
                    matched = True

            if category == "产业政策":
                for sector, codes in _SECTOR_HOLDING_MAP.items():
                    if code in codes:
                        affected.append(p["title"])
                        directions.add("利好")
                        matched = True
                        break
                if not matched and any(kw in title for kw in ["产业", "扶持", "补贴", "规划"]):
                    affected.append(p["title"])
                    directions.add("中性")
                    matched = True

            if category == "监管动态":
                if any(kw in title for kw in ["退市", "IPO", "减持"]):
                    affected.append(p["title"])
                    directions.add("中性")
                    matched = True

        if affected:
            impact = "利好" if "利好" in directions else ("利空" if "利空" in directions else "中性")
            result.append({
                "code": code,
                "name": h.get("名称", ""),
                "impact": impact,
                "confidence": "低",
                "affected_by": affected[:3],
                "reason": f"规则匹配{len(affected)}条政策",
            })

    logger.info(f"[政策] 规则持仓分析: {len(result)} 只")
    return result


def classify_policy(title: str, content: str) -> str:
    """粗略的政策分类（关键词匹配，LLM会在后续报告中细化）"""
    text = (title + " " + content).lower()
    if any(kw in text for kw in ["央行", "降准", "降息", "加息", "MLF", "逆回购", "货币", "利率", "流动性", "美联储", "议息", "通胀"]):
        return "宏观调控"
    if any(kw in text for kw in ["证监会", "监管", "立案", "处罚", "退市", "IPO", "再融资", "减持", "交易规则", "新规"]):
        return "监管动态"
    if any(kw in text for kw in ["税收", "税率", "增值税", "所得税", "关税", "减免税", "退税"]):
        return "税收政策"
    if any(kw in text for kw in ["产业", "扶持", "补贴", "规划", "新能源", "芯片", "AI", "人工智能", "数字经济", "碳中和", "存储芯片", "半导体", "算力", "创新药", "光模块"]):
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

    # 2. 获取历史政策事件（去重用，排除同日的避免自去重）
    data["recent_policies"] = get_recent_policy_events(7)
    recent_titles = {
        p["title"].strip()[:40] for p in data["recent_policies"]
        if p.get("date", "") != trade_date  # 不排除同日数据（本轮采集会覆盖）
    }
    print(f"  {ok} 历史政策: {len(data['recent_policies'])} 条（近7天）"
          f"{'（不含今日' + str(len([p for p in data['recent_policies'] if p.get('date','')==trade_date])) + '条）' if any(p.get('date','')==trade_date for p in data['recent_policies']) else ''}")

    # 3. 采集今日政策新闻（多源fallback）
    news = []
    news_source = ""
    news_errors = []

    # 3a. 东方财富（主力数据源）
    try:
        news = fetch_eastmoney_news()
        if news:
            news_source = "东方财富"
            print(f"  {ok} 东方财富新闻: {len(news)} 条")
        else:
            news_errors.append("东方财富API无数据")
            print(f"  {warn} 东方财富新闻: 无数据")
    except Exception as e:
        news_errors.append(f"东方财富失败: {e}")
        print(f"  {warn} 东方财富新闻: {e}")

    # 3b. 网页抓取（东财API失败时的备用数据源）
    if not news:
        try:
            news = fetch_web_news()
            if news:
                news_source = news[0].get("source", "网页抓取")
                print(f"  {ok} 网页抓取新闻: {len(news)} 条（备用数据源: {news_source}）")
            else:
                news_errors.append("网页抓取无数据")
                print(f"  {warn} 网页抓取: 无数据")
        except Exception as e:
            news_errors.append(f"网页抓取失败: {e}")
            print(f"  {warn} 网页抓取: {e}")

    # 处理采集到的新闻
    if news:
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
                "source": item.get("source", news_source),
                "url": item.get("url", ""),
                "category": category,
                "impact_score": 0,  # LLM会在报告阶段评分
                "impact_sector": "",
                "publish_time": item.get("publish_time", ""),
            })
        print(f"  {ok} 今日政策新闻: {len(data['policy_events'])} 条（去重后）")
    else:
        data["errors"].extend(news_errors)
        print(f"  {fail} 政策新闻: 所有数据源均不可用")

    # 4. 北向资金（流动性观察）
    try:
        data["north_money"] = fetch_north_money(trade_date)
        net = data["north_money"].get("north_net", 0)
        print(f"  {ok} 北向资金: {net:.2f} 亿")
    except Exception as e:
        data["errors"].append(f"北向资金获取失败: {e}")
        print(f"  {fail} 北向资金: {e}")

    # 5. LLM政策×持仓关联分析
    if data["policy_events"] and data["holdings"]:
        try:
            print(f"  [政策] 分析持仓影响...")
            impact = analyze_holdings_impact(data["policy_events"], data["holdings"])
            if impact:
                data["holdings_impact"] = impact
                impacted = len(impact)
                print(f"  {ok} 持仓影响分析: {impacted}/{len(data['holdings'])} 只有关联")
            else:
                print(f"  {warn} 持仓影响分析: 无匹配")
        except Exception as e:
            data["errors"].append(f"持仓影响分析失败: {e}")
            print(f"  {fail} 持仓影响分析: {e}")
    data.setdefault("holdings_impact", [])

    # 6. 写入数据库
    _save_policy_to_db(data)

    # 7. D4-CP: 错误汇总
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
            _db.upsert_policy_event({
                "event_date": trade_date,
                "title": event["title"],
                "content": event["content"],
                "source": event.get("source", ""),
                "category": event.get("category", "宏观政策"),
                "impact_sector": event.get("impact_sector", ""),
                "impact_score": event.get("impact_score", 0),
                "url": event.get("url", ""),
            })
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
    lines.append("> 数据源：证券时报 | 东方财富 | Tushare")
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
        lines.append("## 📋 其他宏观消息")
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

    # 持仓关联（LLM/规则分析）
    holdings = data.get("holdings", [])
    impact_map = {item["code"]: item for item in data.get("holdings_impact", [])}
    if holdings:
        lines.append("## 🔗 对持仓的潜在影响")
        if impact_map:
            lines.append("> 基于今日政策事件的 LLM 影响分析")
        else:
            lines.append("> ⚠️ 暂无政策事件与持仓的自动关联")
        lines.append("")
        lines.append("| 持仓 | 代码 | 关联政策 | 影响方向 | 信心 | 简析 |")
        lines.append("|:----|:----|:--------|:--------|:----|:----|")
        for h in holdings:
            code = h.get("代码", "?")
            imp = impact_map.get(code)
            if imp:
                policies = imp.get("affected_by", [])
                pol_str = "; ".join(policies[:2])
                if len(policies) > 2:
                    pol_str += f"..."
                direction = imp.get("impact", "⏳")
                conf = imp.get("confidence", "低")
                reason = imp.get("reason", "")
                lines.append(f"| {h.get('名称', '?')} | {code} | {pol_str} | {direction} | {conf} | {reason} |")
            else:
                lines.append(f"| {h.get('名称', '?')} | {code} | — | ⏳ | — | 无直接关联 |")
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
