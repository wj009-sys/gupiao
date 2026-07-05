"""
东方财富增强数据源 — 基于 a-stock-data 项目 (V3.3.0)

提供东方财富独有的数据接口（不走 Tushare）：
  1. industry_comparison()       — 行业板块涨跌排名
  2. eastmoney_concept_blocks()  — 个股所属板块/概念归属
  3. eastmoney_stock_info()      — 个股基本面（行业/股本/市值）
  4. eastmoney_global_news()     — 全球财经资讯（7×24）
  5. eastmoney_reports()         — 个股研报列表
  6. download_pdf()              — 研报 PDF 下载
  7. sina_financial_report()     — 新浪财报三表

核心价值：
  - 全部免费无 Key，东财接口已走 em_get 限流
  - 涵盖 Tushare 没有的独特数据（概念板块、研报 PDF、全球资讯）
  - 新浪财报三表提供无需 API Key 的财报数据

来源：a-stock-data (https://github.com/simonlin1212/a-stock-data)
"""

import logging
import time
import random
import re
import json
import uuid
from pathlib import Path
from typing import Optional, List, Dict

import requests

logger = logging.getLogger(__name__)

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/117.0.0.0 Safari/537.36")

# ── 东财限流：复用 eastmoney_get 的设计模式 ──
_EM_SESSION = requests.Session()
_EM_SESSION.headers.update({"User-Agent": UA})

# 连接级自动重试
try:
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
    _adapter = HTTPAdapter(max_retries=Retry(
        total=3, connect=3, backoff_factor=0.6,
        status_forcelist=[429, 500, 502, 503, 504], allowed_methods=["GET"]))
    _EM_SESSION.mount("https://", _adapter)
    _EM_SESSION.mount("http://", _adapter)
except Exception:
    pass  # 老版 urllib3 兼容

_EM_LAST_CALL = [0.0]
EM_MIN_INTERVAL = 1.0


def _em_get(url: str, params: dict = None, headers: dict = None,
            timeout: int = 15, **kwargs) -> requests.Response:
    """东财统一限流请求（内部使用）"""
    wait = EM_MIN_INTERVAL - (time.time() - _EM_LAST_CALL[0])
    if wait > 0:
        time.sleep(wait + random.uniform(0.1, 0.5))
    try:
        return _EM_SESSION.get(url, params=params, headers=headers,
                               timeout=timeout, **kwargs)
    finally:
        _EM_LAST_CALL[0] = time.time()


# ═══════════════════════════════════════════════════════════════════════
# 东财数据中心统一查询
# ═══════════════════════════════════════════════════════════════════════

_DATACENTER_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"


def _eastmoney_datacenter(report_name: str, filter_str: str = "",
                          page_size: int = 50, sort_columns: str = "",
                          sort_types: str = "-1") -> List[dict]:
    """东财数据中心统一查询（内部使用）"""
    params = {
        "reportName": report_name, "columns": "ALL",
        "filter": filter_str, "pageNumber": "1", "pageSize": str(page_size),
        "sortColumns": sort_columns, "sortTypes": sort_types,
        "source": "WEB", "client": "WEB",
    }
    try:
        r = _em_get(_DATACENTER_URL, params=params, timeout=15)
        return (r.json().get("result") or {}).get("data") or []
    except Exception as e:
        logger.warning(f"[eastmoney_plus] datacenter 查询失败 "
                       f"({report_name}): {e}")
        return []


# ═══════════════════════════════════════════════════════════════════════
# 3.7 行业板块排名
# ═══════════════════════════════════════════════════════════════════════

def industry_comparison(top_n: int = 20) -> Dict:
    """
    全行业涨跌幅排名（东财行业板块，~100 个行业）。

    参数:
        top_n: 返回 TOP N 行业
    返回:
        {top: [{rank, name, change_pct, code, up_count, down_count, leader, leader_change}],
         bottom: [...], total: int}

    注意:
        - 东财 push2 可能被住宅 IP 间歇风控，失败时返回空
        - 失败时可降级到现有 Tushare 数据

    D3异常处理:
        | 触发条件         | 一线修复       | 仍失败兜底              |
        |-----------------|--------------|-----------------------|
        | push2 连接失败   | 重试 1 次     | 返回空 {top:[],bottom:[]} |
        | JSON 解析失败    | 捕获异常       | 返回空结构              |
    """
    url = "https://push2.eastmoney.com/api/qt/clist/get"
    params = {
        "pn": "1", "pz": "100", "po": "1", "np": "1",
        "fltt": "2", "invt": "2",
        "fs": "m:90+t:2",
        "fields": "f2,f3,f4,f12,f13,f14,f104,f105,f128,f136,f140",
    }
    headers = {"User-Agent": UA}
    try:
        r = _em_get(url, params=params, headers=headers, timeout=15)
        d = r.json()
    except Exception as e:
        logger.warning(f"[eastmoney_plus] industry_comparison 请求失败: {e}")
        return {"top": [], "bottom": [], "total": 0}

    items = d.get("data", {}).get("diff", [])
    if not items:
        return {"top": [], "bottom": [], "total": 0}

    rows = []
    for i, item in enumerate(items):
        rows.append({
            "rank": i + 1,
            "name": item.get("f14", ""),
            "change_pct": item.get("f3", 0),
            "code": item.get("f12", ""),
            "up_count": item.get("f104", 0),
            "down_count": item.get("f105", 0),
            "leader": item.get("f140", ""),
            "leader_change": item.get("f136", 0),
        })

    return {
        "top": rows[:top_n],
        "bottom": rows[-top_n:],
        "total": len(rows),
    }


# ═══════════════════════════════════════════════════════════════════════
# 3.3 东财 slist — 个股所属板块/概念归属
# ═══════════════════════════════════════════════════════════════════════

def eastmoney_concept_blocks(code: str) -> Dict:
    """
    个股所属板块/概念归属。

    一次请求拿全个股所属的全部板块（行业+概念+地域混合），
    含板块代码(BK码)、当日涨跌幅、板块龙头股。

    参数:
        code: 6位股票代码
    返回:
        {total: int, boards: [{name, code(BK码), change_pct, lead_stock}],
         concept_tags: [板块名...]}

    D3异常处理:
        | 触发条件           | 一线修复       | 仍失败兜底                   |
        |-------------------|--------------|----------------------------|
        | push2 slist 请求失败 | 重试 1 次    | 返回 {total:0, boards:[]}  |
        | JSON diff 为空     | 返回空列表     | {total:0, boards:[]}        |
    """
    market_code = 1 if code.startswith("6") else 0
    params = {
        "fltt": "2", "invt": "2",
        "secid": f"{market_code}.{code}",
        "spt": "3", "pi": "0", "pz": "200", "po": "1",
        "fields": "f12,f14,f3,f128",
    }
    headers = {"User-Agent": UA, "Referer": "https://quote.eastmoney.com/"}
    try:
        r = _em_get("https://push2.eastmoney.com/api/qt/slist/get",
                    params=params, headers=headers, timeout=15)
        d = r.json()
    except Exception as e:
        logger.warning(f"[eastmoney_plus] concept_blocks 请求失败 ({code}): {e}")
        return {"total": 0, "boards": [], "concept_tags": []}

    diff = (d.get("data") or {}).get("diff") or {}
    items = diff.values() if isinstance(diff, dict) else diff
    boards = []
    for it in items:
        boards.append({
            "name": it.get("f14", ""),
            "code": it.get("f12", ""),
            "change_pct": it.get("f3", ""),
            "lead_stock": it.get("f128", ""),
        })
    return {
        "total": len(boards),
        "boards": boards,
        "concept_tags": [b["name"] for b in boards],
    }


# ═══════════════════════════════════════════════════════════════════════
# 6.3 东财个股基本面（直连 push2 API）
# ═══════════════════════════════════════════════════════════════════════

def eastmoney_stock_info(code: str) -> Dict:
    """
    东财个股基本面信息。

    参数:
        code: 6位股票代码
    返回:
        {code, name, industry, total_shares, float_shares,
         mcap, float_mcap, list_date, price}
    """
    market_code = 1 if code.startswith("6") else 0
    url = "https://push2.eastmoney.com/api/qt/stock/get"
    params = {
        "fltt": "2", "invt": "2",
        "fields": "f57,f58,f84,f85,f127,f116,f117,f189,f43",
        "secid": f"{market_code}.{code}",
    }
    headers = {"User-Agent": UA}
    try:
        r = _em_get(url, params=params, headers=headers, timeout=10)
        d = r.json().get("data", {})
    except Exception as e:
        logger.warning(f"[eastmoney_plus] stock_info 请求失败 ({code}): {e}")
        return {}

    return {
        "code": d.get("f57", ""),
        "name": d.get("f58", ""),
        "industry": d.get("f127", ""),
        "total_shares": d.get("f84", 0),
        "float_shares": d.get("f85", 0),
        "mcap": d.get("f116", 0),
        "float_mcap": d.get("f117", 0),
        "list_date": str(d.get("f189", "")),
        "price": d.get("f43", 0),
    }


# ═══════════════════════════════════════════════════════════════════════
# 5.3 东财全球资讯（7×24）
# ═══════════════════════════════════════════════════════════════════════

def eastmoney_global_news(page_size: int = 50) -> List[Dict]:
    """
    东方财富全球财经资讯（7×24 滚动）。

    参数:
        page_size: 返回条数
    返回:
        [{title, summary, time}]
    """
    url = "https://np-weblist.eastmoney.com/comm/web/getFastNewsList"
    params = {
        "client": "web", "biz": "web_724",
        "fastColumn": "102", "sortEnd": "",
        "pageSize": str(page_size),
        "req_trace": str(uuid.uuid4()),
    }
    headers = {"User-Agent": UA, "Referer": "https://kuaixun.eastmoney.com/"}
    try:
        r = _em_get(url, params=params, headers=headers, timeout=10)
        d = r.json()
    except Exception as e:
        logger.warning(f"[eastmoney_plus] global_news 请求失败: {e}")
        return []

    rows = []
    for item in d.get("data", {}).get("fastNewsList", []):
        rows.append({
            "title": item.get("title", ""),
            "summary": item.get("summary", "")[:200],
            "time": item.get("showTime", ""),
        })
    return rows


# ═══════════════════════════════════════════════════════════════════════
# 2.1 东财研报 API
# ═══════════════════════════════════════════════════════════════════════

_REPORT_API = "https://reportapi.eastmoney.com/report/list"
_PDF_TPL = "https://pdf.dfcfw.com/pdf/H3_{info_code}_1.pdf"


def eastmoney_reports(code: str, max_pages: int = 5) -> List[Dict]:
    """
    拉取指定股票的研报列表。

    参数:
        code: 6位股票代码
        max_pages: 最大页数（每页 100 条）
    返回:
        [{title, publishDate, orgSName, infoCode, predictThisYearEps,
          predictNextYearEps, predictNextTwoYearEps, emRatingName, indvInduName}]
    """
    all_records = []
    for page in range(1, max_pages + 1):
        params = {
            "industryCode": "*", "pageSize": "100", "industry": "*",
            "rating": "*", "ratingChange": "*",
            "beginTime": "2000-01-01", "endTime": "2030-01-01",
            "pageNo": str(page), "fields": "", "qType": "0",
            "orgCode": "", "code": code, "rcode": "",
            "p": str(page), "pageNum": str(page), "pageNumber": str(page),
        }
        try:
            r = _em_get(_REPORT_API, params=params,
                        headers={"Referer": "https://data.eastmoney.com/"}, timeout=30)
            d = r.json()
            rows = d.get("data") or []
            if not rows:
                break
            all_records.extend(rows)
            if page >= (d.get("TotalPage", 1) or 1):
                break
        except Exception as e:
            logger.warning(f"[eastmoney_plus] reports 请求失败 ({code}, page={page}): {e}")
            break
    return all_records


def download_pdf(record: dict, target_dir: str = "./reports/研报PDF") -> Optional[str]:
    """
    下载单份研报 PDF。

    参数:
        record: eastmoney_reports() 返回的单条记录
        target_dir: 保存目录
    返回:
        保存路径，失败返回 None
    """
    info_code = record.get("infoCode", "")
    if not info_code:
        return None
    date = (record.get("publishDate") or "")[:10]
    org = re.sub(r'[\\/:*?"<>|]', "_", record.get("orgSName") or "未知")[:40]
    title = re.sub(r'[\\/:*?"<>|]', "_", record.get("title", ""))[:80]
    fname = f"{date}_{org}_{title}.pdf"
    target = Path(target_dir) / fname
    if target.exists():
        return str(target)

    url = _PDF_TPL.format(info_code=info_code)
    try:
        r = _em_get(url, headers={"Referer": "https://data.eastmoney.com/"}, timeout=60)
        if r.status_code == 200 and len(r.content) >= 1024:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(r.content)
            return str(target)
    except Exception as e:
        logger.warning(f"[eastmoney_plus] PDF 下载失败 ({info_code}): {e}")
    return None


# ═══════════════════════════════════════════════════════════════════════
# 6.4 新浪财报三表
# ═══════════════════════════════════════════════════════════════════════

def sina_financial_report(code: str, report_type: str = "lrb",
                          num: int = 8) -> List[Dict]:
    """
    新浪财报三表。

    参数:
        code: 6位代码
        report_type: "fzb"(资产负债表) / "lrb"(利润表) / "llb"(现金流量表)
        num: 取最近 N 期（默认 8 期）
    返回:
        按报告期倒序的记录列表，每期为 dict：
          {"报告期": "2026-03-31", "<科目>": "<值>", "<科目>_同比": <同比>}
    """
    prefix = "sh" if code.startswith("6") else "sz"
    paper_code = f"{prefix}{code}"
    url = ("https://quotes.sina.cn/cn/api/openapi.php/"
           "CompanyFinanceService.getFinanceReport2022")
    params = {
        "paperCode": paper_code, "source": report_type,
        "type": "0", "page": "1", "num": str(num),
    }
    headers = {"User-Agent": UA}
    try:
        r = requests.get(url, params=params, headers=headers, timeout=15)
        report_list = (r.json().get("result", {}).get("data", {})
                       .get("report_list", {})) or {}
    except Exception as e:
        logger.warning(f"[eastmoney_plus] sina 财报请求失败 ({code}/{report_type}): {e}")
        return []

    rows = []
    for period in sorted(report_list.keys(), reverse=True)[:num]:
        obj = report_list[period]
        rec = {"报告期": f"{period[:4]}-{period[4:6]}-{period[6:8]}"}
        for it in obj.get("data", []) or []:
            title = it.get("item_title", "")
            if not title or it.get("item_value") is None:
                continue
            rec[title] = it.get("item_value")
            tongbi = it.get("item_tongbi")
            if tongbi not in (None, ""):
                rec[title + "_同比"] = tongbi
        rows.append(rec)
    return rows


if __name__ == "__main__":
    import json

    # 1. 行业排名
    print("=== 行业板块排名 ===")
    comp = industry_comparison(10)
    print(f"共 {comp['total']} 个行业")
    for r in comp["top"][:5]:
        print(f"  {r['rank']}. {r['name']}: {r['change_pct']}% "
              f"涨{r['up_count']}跌{r['down_count']} 领涨{r['leader']}")

    # 2. 概念板块
    print("\n=== 概念板块归属 ===")
    for code in ["600519", "000858", "688017"]:
        blocks = eastmoney_concept_blocks(code)
        print(f"{code}: {blocks['total']} 个板块")
        print(f"  {', '.join(blocks['concept_tags'][:8])}")

    # 3. 个股基本面
    print("\n=== 个股基本面 ===")
    info = eastmoney_stock_info("688017")
    print(f"{info.get('name','')}: 行业={info.get('industry','')} "
          f"总市值={info.get('mcap',0)/1e8:.0f}亿")

    # 4. 全球资讯
    print("\n=== 全球资讯 ===")
    news = eastmoney_global_news(5)
    for n in news[:3]:
        print(f"  {n['time']} | {n['title'][:60]}")

    # 5. 研报
    print("\n=== 研报 ===")
    reports = eastmoney_reports("688017", max_pages=1)
    print(f"共 {len(reports)} 篇研报")
    for r in reports[:3]:
        print(f"  {r.get('publishDate','')[:10]} | {r.get('orgSName','')} | "
              f"{r.get('title','')[:40]} | 评级{r.get('emRatingName','')}")

    # 6. 新浪财报
    print("\n=== 新浪利润表 ===")
    lrb = sina_financial_report("600519", "lrb", 3)
    for item in lrb:
        print(f"  {item.get('报告期','')}: "
              f"净利润={item.get('净利润','')} "
              f"营收={item.get('营业总收入','')}")
