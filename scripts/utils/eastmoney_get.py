"""
东方财富数据 API 限流网关 — 借鉴 a-stock-data 的 em_get() 反爬限流设计

提供统一的限流 HTTP 请求接口，通过串行化 + 固定间隔 + 随机抖动，
在东方财富的反爬阈值内安全获取数据。

实测东财封禁阈值（来自 a-stock-data 项目实测）：
  - >5 req/s → 封禁
  - >=10 并发连接 → 封禁
  - >=200 请求/min → 封禁
  - >=300 请求/5min → 封禁

本网关强制：
  - 串行请求（无并发）
  - >=1.0s 请求间隔 + 0.1-0.5s 随机抖动
  - Session Keep-Alive 复用

D3异常处理表：
| 触发条件 | 一线修复 | 仍失败兜底 |
|---------|---------|-----------|
| HTTP 403 (被封禁) | 等待 60s 后重试 1 次 | 返回空 dict/df，调用方降级到其他数据源 |
| HTTP 429 (频率超限) | 等待 30s 后重试 1 次 | 返回空 dict/df |
| 网络超时/连接重置 | 重试 2 次，间隔 2s/5s | 返回空 dict/df |
| JSON 解析失败 | 检查响应是否为 HTML 反爬页面 | 返回空 dict/df |

D4 CHECKPOINT:
- CP1-限流检查：每次请求前检查与上次请求的时间间隔是否 >=1s
- CP2-响应检查：检查响应状态码，非 200 按对应错误处理
- CP3-格式检查：检查响应是否为合法 JSON

D9反例：
- 不要并发请求东财 API（会触发并发封禁）
- 不要使用同一 Session 高频请求（超过 5 req/s 必封）
- 不要忽略 403 响应继续重试（退避至少 60s）
"""
import time
import random
import logging
from typing import Optional, Dict, Any

import requests

logger = logging.getLogger(__name__)

# ── 全局限流状态 ──
_last_request_time: float = 0.0
_session: Optional[requests.Session] = None

# ── 限流参数 ──
MIN_INTERVAL = 1.0        # 最小请求间隔（秒）
JITTER_MIN = 0.1          # 抖动下限（秒）
JITTER_MAX = 0.5          # 抖动上限（秒）
BAN_BACKOFF = 60.0        # 被封禁后等待（秒）
RATE_BACKOFF = 30.0       # 超频后等待（秒）

# ── 东财 API 基础 URL ──
EASTMONEY_BASES = {
    "datacenter": "https://datacenter.eastmoney.com",
    "push2": "https://push2.eastmoney.com",
    "push2his": "https://push2his.eastmoney.com",
    "search": "https://search-api-web.eastmoney.com",
    "reportapi": "https://reportapi.eastmoney.com",
    "emdiscs": "https://emdiscs.eastmoney.com",
    "np-anotice": "https://np-anotice.eastmoney.com",
    "so": "https://so.eastmoney.com",
}


def _get_session() -> requests.Session:
    """获取/创建全局 Session（Keep-Alive + SOCKS代理）"""
    global _session
    if _session is None:
        from scripts.utils._proxy import get_session_with_proxy
        _session = get_session_with_proxy()
        _session.headers.update({
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Connection": "keep-alive",
            "Referer": "https://data.eastmoney.com/",
        })
    return _session


def em_get(
    endpoint: str,
    params: Optional[Dict[str, Any]] = None,
    base_key: str = "datacenter",
    timeout: int = 15,
) -> Optional[Dict[str, Any]]:
    """
    限流的东方财富 HTTP GET 请求

    Args:
        endpoint: API 路径（如 /api/data/v1/get）
        params: 查询参数 dict
        base_key: 基础 URL 键名（见 EASTMONEY_BASES）
        timeout: 超时秒数

    Returns:
        解析后的 JSON dict，失败返回 None

    用法:
        data = em_get("/api/data/v1/get", params={"reportName": "RPT_..."})
    """
    global _last_request_time

    base_url = EASTMONEY_BASES.get(base_key)
    if base_url is None:
        logger.error(f"[em_get] 未知端点: {base_key}，可用: {list(EASTMONEY_BASES.keys())}")
        return None

    url = f"{base_url}{endpoint}"
    session = _get_session()

    # ── CP1: 限流 — 确保 >= 1s + 抖动 ──
    elapsed = time.time() - _last_request_time
    if elapsed < MIN_INTERVAL:
        sleep_time = MIN_INTERVAL - elapsed + random.uniform(JITTER_MIN, JITTER_MAX)
        time.sleep(sleep_time)
    elif _last_request_time > 0:
        time.sleep(random.uniform(JITTER_MIN, JITTER_MAX))

    _last_request_time = time.time()

    # ── 执行请求（含重试） ──
    for attempt in range(3):
        try:
            resp = session.get(url, params=params, timeout=timeout)
            # ── CP2: 响应检查 ──
            if resp.status_code == 403:
                logger.warning(f"[em_get] 403 被封禁 (attempt {attempt+1}/3)，等待 {BAN_BACKOFF}s")
                if attempt < 2:
                    time.sleep(BAN_BACKOFF)
                    continue
                return None
            if resp.status_code == 429:
                logger.warning(f"[em_get] 429 超频 (attempt {attempt+1}/3)，等待 {RATE_BACKOFF}s")
                if attempt < 2:
                    time.sleep(RATE_BACKOFF)
                    continue
                return None
            if resp.status_code != 200:
                logger.warning(f"[em_get] HTTP {resp.status_code} (attempt {attempt+1}/3)")
                if attempt < 2:
                    time.sleep(2 * (attempt + 1))
                    continue
                return None

            # ── CP3: JSON 解析 ──
            try:
                return resp.json()
            except ValueError:
                logger.warning(f"[em_get] JSON 解析失败 (attempt {attempt+1}/3)，可能为反爬页面")
                if attempt < 2:
                    time.sleep(2 * (attempt + 1))
                    continue
                return None

        except requests.exceptions.Timeout:
            logger.warning(f"[em_get] 超时 (attempt {attempt+1}/3)")
            if attempt < 2:
                time.sleep(2 * (attempt + 1))
                continue
            return None
        except requests.exceptions.ConnectionError:
            logger.warning(f"[em_get] 连接重置 (attempt {attempt+1}/3)")
            if attempt < 2:
                time.sleep(5)
                continue
            return None
        except Exception as e:
            logger.error(f"[em_get] 未知错误: {e}")
            return None

    return None


def reset_rate_limiter():
    """重置限流状态（测试/恢复用）"""
    global _last_request_time
    global _session
    _last_request_time = 0.0
    if _session:
        _session.close()
    _session = None


# ── 便捷函数：获取东方财富通用数据接口 ──

def get_dragon_tiger(trade_date: str) -> Optional[Dict[str, Any]]:
    """
    获取龙虎榜数据（东方财富 API）

    Args:
        trade_date: 日期 YYYYMMDD

    Returns:
        龙虎榜 JSON 或 None
    """
    # 东财龙虎榜 API: datacenter api
    date_fmt = f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:8]}"
    params = {
        "reportName": "RPT_DAILY_BILLBOARD",
        "columns": "ALL",
        "filter": f'(TRADE_DATE=\'{date_fmt}\')',
        "pageNumber": 1,
        "pageSize": 100,
        "sortTypes": -1,
        "sortColumns": "NET_BUY_AMT",
        "source": "WEB",
        "client": "WEB",
    }
    return em_get("/api/data/v1/get", params=params, base_key="datacenter")


def get_moneyflow_stock(ts_code: str, trade_date: str) -> Optional[Dict[str, Any]]:
    """
    获取个股资金流向（东方财富 API）

    Args:
        ts_code: Tushare 代码 000001.SZ
        trade_date: 日期 YYYYMMDD

    Returns:
        资金流向 JSON 或 None
    """
    # 提取纯代码
    code = ts_code[:6]
    date_fmt = f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:8]}"
    params = {
        "reportName": "RPT_MONEY_FLOW_STOCK",
        "columns": "ALL",
        "filter": f'(SECURITY_CODE="{code}")(TRADE_DATE="{date_fmt}")',
        "pageNumber": 1,
        "pageSize": 10,
        "sortTypes": -1,
        "sortColumns": "",
        "source": "WEB",
        "client": "WEB",
    }
    return em_get("/api/data/v1/get", params=params, base_key="datacenter")


def get_lockup_calendar(trade_date: str) -> Optional[Dict[str, Any]]:
    """
    获取限售股解禁日历（东方财富 API）

    Args:
        trade_date: 日期 YYYYMMDD

    Returns:
        解禁日历 JSON 或 None
    """
    date_fmt = f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:8]}"
    params = {
        "reportName": "RPT_LOCKUP_SHARE",
        "columns": "ALL",
        "filter": f'(UNLOCK_DATE=\'{date_fmt}\')',
        "pageNumber": 1,
        "pageSize": 100,
        "sortTypes": -1,
        "sortColumns": "UNLOCK_SHARE_AMOUNT",
        "source": "WEB",
        "client": "WEB",
    }
    return em_get("/api/data/v1/get", params=params, base_key="datacenter")


def get_margin_detail(ts_code: str, start_date: str = None, end_date: str = None) -> Optional[Dict[str, Any]]:
    """
    获取个股融资融券明细（东方财富 API）

    Args:
        ts_code: Tushare 代码
        start_date: 开始日期 YYYYMMDD
        end_date: 结束日期 YYYYMMDD

    Returns:
        融资融券 JSON 或 None
    """
    code = ts_code[:6]
    params = {
        "reportName": "RPT_MARGIN_STOCK",
        "columns": "ALL",
        "filter": f'(SECURITY_CODE="{code}")',
        "pageNumber": 1,
        "pageSize": 100,
        "sortTypes": -1,
        "sortColumns": "TRADE_DATE",
        "source": "WEB",
        "client": "WEB",
    }
    if start_date:
        sd = f"{start_date[:4]}-{start_date[4:6]}-{start_date[6:8]}"
        params["filter"] += f'(TRADE_DATE>="{sd}")'
    if end_date:
        ed = f"{end_date[:4]}-{end_date[4:6]}-{end_date[6:8]}"
        params["filter"] += f'(TRADE_DATE<="{ed}")'
    return em_get("/api/data/v1/get", params=params, base_key="datacenter")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    # 测试：龙虎榜
    print("=== 测试: 龙虎榜 ===")
    lb = get_dragon_tiger("20260703")
    print(f"龙虎榜数据: {'✅ 获取成功' if lb else '❌ 失败'}")

    # 测试: 个股资金流向
    print("\n=== 测试: 个股资金流向 (600519.SH) ===")
    mf = get_moneyflow_stock("600519.SH", "20260703")
    print(f"资金流向: {'✅ 获取成功' if mf else '❌ 失败'}")

    # 测试: 限流
    print("\n=== 测试: 限流 (5次快速请求) ===")
    for i in range(5):
        t = time.time()
        lb = get_dragon_tiger("20260703")
        dt = time.time() - t
        print(f"  请求 {i+1}: {dt:.2f}s{' ✅' if lb else ' ❌'}")
    print("限流测试完成 — 每次请求应 >= 1.0s 间隔")
