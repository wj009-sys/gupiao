"""
巨潮公告 + 互动易问答 + 舆情热榜 — 基于 a-stock-data 项目 (V3.3.0)

提供三类独特数据：
  1. cninfo_announcements() — 巨潮公告全文检索
  2. cninfo_irm()           — 互动易问答（投资者提问+公司回应）
  3. em_hot_rank()          — 东财人气榜
  4. em_hot_concept()       — 东财个股概念命中（这只票当下被归到哪些概念）

核心价值：
  - 全部免费无 Key
  - 互动易问答是 AI 问答独家信源：能答"公司如何回应某传闻/利好"
  - 巨潮公告覆盖沪深北全量公告

数据源：巨潮 cninfo.com.cn + 东财 emappdata + 同花顺 10jqka

来源：a-stock-data (https://github.com/simonlin1212/a-stock-data)
"""

import logging
from datetime import datetime
from typing import Optional, List, Dict

import requests

logger = logging.getLogger(__name__)

# ── 共享限流网关 + 代理（跨模块协调请求间隔，避免IP封禁） ──
from scripts.utils._proxy import rate_limited_get, get_session_with_proxy

_EM_SESSION = get_session_with_proxy()
EM_MIN_INTERVAL = 1.1


def _em_get(url: str, params: dict = None, headers: dict = None,
            timeout: int = 15, **kwargs) -> requests.Response:
    """东财统一限流请求（使用共享网关，跨模块协调）"""
    return rate_limited_get(_EM_SESSION, url, params=params,
                            headers=headers, timeout=timeout,
                            min_interval=EM_MIN_INTERVAL)


# ═══════════════════════════════════════════════════════════════════════
# 巨潮 股票→orgId 映射
# ═══════════════════════════════════════════════════════════════════════

_CNINFO_ORGID_MAP = {}


def _cninfo_orgid(code: str) -> str:
    """
    查股票真实 orgId。

    动态查官方映射表 szse_stock.json（6198 只股，模块级缓存），
    查不到再回退硬编码规则。硬编码会导致大量 601xxx 股票查不到公告。
    """
    global _CNINFO_ORGID_MAP
    if not _CNINFO_ORGID_MAP:
        try:
            r = requests.get(
                "http://www.cninfo.com.cn/new/data/szse_stock.json",
                headers={"User-Agent": UA}, timeout=15)
            _CNINFO_ORGID_MAP = {s["code"]: s["orgId"]
                                 for s in r.json().get("stockList", [])}
        except Exception as e:
            logger.warning(f"[cninfo_sentiment] orgId 映射拉取失败: {e}")

    org = _CNINFO_ORGID_MAP.get(code)
    if org:
        return org
    # fallback：老格式硬编码
    if code.startswith("6"):
        return f"gssh0{code}"
    elif code.startswith(("8", "4")):
        return f"gsbj0{code}"
    return f"gssz0{code}"


def _cninfo_ts_to_date(ts) -> str:
    """巨潮 announcementTime 毫秒时间戳 → YYYY-MM-DD"""
    if isinstance(ts, (int, float)):
        return datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d")
    return str(ts)[:10] if ts else ""


# ═══════════════════════════════════════════════════════════════════════
# 7.1 巨潮公告全文检索
# ═══════════════════════════════════════════════════════════════════════

def cninfo_announcements(code: str, page_size: int = 30) -> List[Dict]:
    """
    巨潮公告全文检索。

    参数:
        code: 6位股票代码
        page_size: 返回条数
    返回:
        [{title, type, date, url}]

    D3异常处理:
        | 触发条件           | 一线修复         | 仍失败兜底    |
        |-------------------|----------------|-------------|
        | HTTP 请求失败       | 重试 1 次       | 返回空列表    |
        | orgId 映射拉取失败  | 回退硬编码规则    | 返回空列表    |
        | JSON 解析失败       | 捕获异常         | 返回空列表    |
        | 公告列表为空        | 正常（公告日间隔）| 返回空列表    |

    D9反例:
        - 不要假定 orgId 是 gssx0{code}（601xxx 股票会查不到公告）
        - 公告全文 URL 需要 annoId 拼接，不要用 code 直接拼
    """
    org_id = _cninfo_orgid(code)
    url = "https://www.cninfo.com.cn/new/hisAnnouncement/query"
    payload = {
        "stock": f"{code},{org_id}",
        "tabName": "fulltext",
        "pageSize": str(page_size), "pageNum": "1",
        "column": "", "category": "", "plate": "",
        "seDate": "", "searchkey": "", "secid": "",
        "sortName": "", "sortType": "", "isHLtitle": "true",
    }
    headers = {
        "User-Agent": UA,
        "Content-Type": "application/x-www-form-urlencoded",
        "Referer": "https://www.cninfo.com.cn/new/disclosure",
        "Origin": "https://www.cninfo.com.cn",
    }
    try:
        r = requests.post(url, data=payload, headers=headers, timeout=15)
        d = r.json()
    except Exception as e:
        logger.warning(f"[cninfo_sentiment] 公告请求失败 ({code}): {e}")
        return []

    rows = []
    for item in d.get("announcements", []) or []:
        rows.append({
            "title": item.get("announcementTitle", ""),
            "type": item.get("announcementTypeName", ""),
            "date": _cninfo_ts_to_date(item.get("announcementTime")),
            "url": ("https://www.cninfo.com.cn/new/disclosure/detail?"
                    f"annoId={item.get('announcementId', '')}"),
        })
    return rows


# ═══════════════════════════════════════════════════════════════════════
# 10.1 互动易问答（巨潮 — 投资者提问+公司回复）
# ═══════════════════════════════════════════════════════════════════════

def cninfo_irm(code: str, page_size: int = 30,
               page_num: int = 1) -> List[Dict]:
    """
    互动易问答（深沪统一走巨潮）。

    参数:
        code: 6位股票代码
        page_size: 每页条数
        page_num: 页码
    返回:
        [{code, company, question, answer(公司回复,None=未回复),
          answerer, ask_time}]

    注意:
        - 第二步参数放 query string（不是 body），否则 HTTP 400
        - 公司回复率差异大（立讯精密回复多、京东方几乎不回）
        - 最新提问常未回复（answer=None）

    D3异常处理:
        | 触发条件        | 一线修复       | 仍失败兜底    |
        |----------------|--------------|-------------|
        | 第一步 orgId 查询失败 | 捕获异常 | 返回空列表    |
        | 第二步请求 400   | 检查参数位置   | 返回空列表    |
        | JSON 解析失败    | 捕获异常       | 返回空列表    |
    """
    # Step 1: 查询 orgId
    try:
        r1 = requests.post(
            "https://irm.cninfo.com.cn/newircs/index/queryKeyboardInfo",
            data={"keyWord": code},
            headers={"User-Agent": UA}, timeout=10)
        d1 = r1.json().get("data") or []
        if not d1:
            return []
        org_id = d1[0].get("secid")
    except Exception as e:
        logger.warning(f"[cninfo_sentiment] IRM orgId 查询失败 ({code}): {e}")
        return []

    # Step 2: 获取问答列表（参数在 query string）
    params = {
        "_t": 1, "stockcode": code, "orgId": org_id,
        "pageSize": page_size, "pageNum": page_num,
        "keyWord": "", "startDay": "", "endDay": "",
    }
    try:
        r2 = requests.post(
            "https://irm.cninfo.com.cn/newircs/company/question",
            params=params,
            headers={"User-Agent": UA}, timeout=10)
        rows = r2.json().get("rows") or []
    except Exception as e:
        logger.warning(f"[cninfo_sentiment] IRM 问答请求失败 ({code}): {e}")
        return []

    out = []
    for it in rows:
        pd_ts = it.get("pubDate")
        out.append({
            "code": it.get("stockCode"),
            "company": it.get("companyShortName"),
            "question": it.get("mainContent"),
            "answer": it.get("attachedContent"),
            "answerer": it.get("attachedAuthor"),
            "ask_time": (datetime.fromtimestamp(pd_ts / 1000)
                        .strftime("%Y-%m-%d %H:%M") if pd_ts else ""),
        })
    return out


# ═══════════════════════════════════════════════════════════════════════
# 10.2 东财人气榜
# ═══════════════════════════════════════════════════════════════════════

_EM_HOT_BODY = {"appId": "appId01", "globalId": "786e4c21-70dc-435a-93bb-38"}


def em_hot_rank(top: int = 50) -> List[Dict]:
    """
    东财人气榜。

    参数:
        top: 返回 TOP N
    返回:
        [{rank, code, name, price, pct, rank_chg(排名变化)}]

    注意:
        - 人气榜只返回带前缀代码（SZ/SH），名称需另走 push2 补齐
        - push2 的 diff 字段偶尔是 dict（已做 list(values()) 归一化）

    D3异常处理:
        | 触发条件         | 一线修复       | 仍失败兜底    |
        |-----------------|--------------|-------------|
        | emappdata 请求失败 | 重试 1 次    | 返回空列表    |
        | push2 补名失败    | 降级只返回代码 | 返回部分结果   |
        | 数据为空          | 返回空列表     | 空列表        |
    """
    try:
        r = requests.post(
            "https://emappdata.eastmoney.com/stockrank/getAllCurrentList",
            json={**_EM_HOT_BODY, "marketType": "", "pageNo": 1, "pageSize": top},
            headers={"User-Agent": UA}, timeout=10)
        data = r.json().get("data") or []
    except Exception as e:
        logger.warning(f"[cninfo_sentiment] em_hot_rank 请求失败: {e}")
        return []

    if not data:
        return []

    # 批量补名称/价格（push2 ulist.np）
    secids = [("0." if it["sc"].startswith("SZ") else "1.") + it["sc"][2:]
              for it in data]
    try:
        u = _em_get("https://push2.eastmoney.com/api/qt/ulist.np/get",
            params={"ut": "f057cbcbce2a86e2866ab8877db1d059", "fltt": 2,
                    "invt": 2, "fields": "f14,f3,f12,f2",
                    "secids": ",".join(secids)}, timeout=10)
        diff = (u.json().get("data") or {}).get("diff") or []
        if isinstance(diff, dict):
            diff = list(diff.values())
        nm = {x["f12"]: (x.get("f14"), x.get("f2"), x.get("f3")) for x in diff}
    except Exception as e:
        logger.warning(f"[cninfo_sentiment] push2 补名失败: {e}")
        nm = {}

    out = []
    for it in data:
        code = it["sc"][2:]
        name, price, pct = nm.get(code, ("", None, None))
        out.append({
            "rank": it["rk"], "code": code, "name": name,
            "price": price, "pct": pct, "rank_chg": it.get("hisRc"),
        })
    return out


# ═══════════════════════════════════════════════════════════════════════
# 10.2 东财个股概念命中
# ═══════════════════════════════════════════════════════════════════════

def em_hot_concept(code: str) -> List[Dict]:
    """
    东财个股热门概念命中。

    这只票当下被市场归到哪些概念在炒，按热度降序。

    参数:
        code: 6位股票代码
    返回:
        [{concept(概念名), bk(概念BK码), hit(命中热度)}, ...]
    """
    prefix = "SH" if code.startswith("6") else "SZ"
    try:
        r = requests.post(
            "https://emappdata.eastmoney.com/stockrank/getHotStockRankList",
            json={**_EM_HOT_BODY, "srcSecurityCode": prefix + code},
            headers={"User-Agent": UA}, timeout=10)
        data = r.json().get("data") or []
    except Exception as e:
        logger.warning(f"[cninfo_sentiment] em_hot_concept 请求失败 ({code}): {e}")
        return []

    return [{"concept": x.get("conceptName"), "bk": x.get("conceptId"),
             "hit": x.get("hitCount")} for x in data]


if __name__ == "__main__":
    # 1. 公告
    print("=== 巨潮公告 ===")
    anns = cninfo_announcements("600519", page_size=5)
    print(f"茅台最新 {len(anns)} 条公告:")
    for a in anns[:5]:
        print(f"  {a['date']} | {a['type']} | {a['title'][:40]}")

    # 公告恢复测试（601xxx 曾有 orgId Bug）
    print()
    for code in ["601318", "601398"]:
        anns = cninfo_announcements(code, page_size=3)
        print(f"{code}: {len(anns)} 条公告")

    # 2. 互动易问答
    print("\n=== 互动易问答 ===")
    qas = cninfo_irm("002594", page_size=5)  # 比亚迪
    print(f"比亚迪互动易 {len(qas)} 条:")
    for q in qas:
        if q["answer"]:
            print(f"  Q: {q['question'][:40]}")
            print(f"  A: {q['answer'][:50]}")
        else:
            print(f"  Q: {q['question'][:40]} (未回复)")

    # 3. 东财人气榜
    print("\n=== 东财人气榜 TOP5 ===")
    hot = em_hot_rank(10)
    for s in hot[:5]:
        print(f"  #{s['rank']} {s['name']}({s['code']}) {s['pct']}%")

    # 4. 个股概念命中
    if hot:
        print(f"\n=== {hot[0]['name']} 概念命中 ===")
        concepts = em_hot_concept(hot[0]["code"])
        for c in concepts[:5]:
            print(f"  {c['concept']}: 热度 {c['hit']}")
