"""
共享代理模块 — 统一读取 SOCKS_PROXY 配置

所有需要访问东财/同花顺/腾讯等外部API的模块，从此处导入代理配置。
避免每个模块各自读取 settings.local.json，实现一处配置全局生效。

用法:
    from scripts.utils._proxy import get_proxies, get_session_with_proxy

    # 获取代理配置
    proxies = get_proxies()  # {"http": "socks5://...", "https": "socks5://..."} 或 None

    # 创建带代理的 requests Session
    session = get_session_with_proxy()
    session.get("https://push2.eastmoney.com/...")

D3异常处理:
    | 触发条件              | 一线修复              | 仍失败兜底          |
    |---------------------|---------------------|-------------------|
    | settings.local.json 不存在 | 静默返回 None        | 调用方无需代理直连  |
    | JSON解析失败          | 打印WARN，返回None    | 调用方无需代理直连  |
    | SOCKS_PROXY 环境变量不合法 | 返回 None            | 调用方无需代理直连  |

D9反例:
    - 不要在模块级别调用 get_proxies()（导入时执行文件I/O），延迟到使用时才调用
    - 不要在多处硬编码 settings.local.json 路径，统一用此模块
"""

import os
import json
from typing import Optional, Dict
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# 项目根目录（向上三级：scripts/utils -> scripts -> 根目录）
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

# 缓存代理配置，避免每次调用都读文件
_proxy_cache = None
_proxy_loaded = False


def get_proxies() -> Optional[Dict[str, str]]:
    """
    获取代理配置（延迟加载，缓存结果）

    从 .claude/settings.local.json 的 env.SOCKS_PROXY 读取。
    支持 socks5/http/https 代理格式。

    Returns:
        {"http": "socks5://127.0.0.1:1080", "https": "socks5://127.0.0.1:1080"}
        或 None（未配置）
    """
    global _proxy_cache, _proxy_loaded
    if _proxy_loaded:
        return _proxy_cache

    _proxy_loaded = True
    settings_paths = [
        os.path.join(_PROJECT_ROOT, ".claude", "settings.local.json"),
        os.path.join(_PROJECT_ROOT, ".claude", "settings.json"),
    ]

    for sp in settings_paths:
        if os.path.exists(sp):
            try:
                with open(sp, "r", encoding="utf-8") as f:
                    s = json.load(f)
                    proxy_url = s.get("env", {}).get("SOCKS_PROXY", "")
                    if proxy_url:
                        _proxy_cache = {"http": proxy_url, "https": proxy_url}
                        return _proxy_cache
            except Exception as e:
                print(f"  [WARN] 读取代理配置失败 ({sp}): {e}")

    _proxy_cache = None
    return None


_direct_session = None  # 直连Session缓存（代理降级用）


def _get_direct_session(timeout: int = 15, retries: int = 3) -> object:
    """创建无代理直连 Session"""
    global _direct_session
    if _direct_session is not None:
        return _direct_session
    import requests as _req
    session = _req.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    })
    try:
        adapter = HTTPAdapter(max_retries=Retry(
            total=retries, connect=retries, backoff_factor=0.6,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET", "POST"],
        ))
        session.mount("https://", adapter)
        session.mount("http://", adapter)
    except Exception:
        pass
    _direct_session = session
    return session


def get_session_with_proxy(timeout: int = 15, retries: int = 3) -> object:
    """
    创建带代理和重试机制的 requests Session

    集成:
    - SOCKS_PROXY 代理（如有配置，自动降级直连）
    - 连接级自动重试（3次）
    - 统一超时设置

    Args:
        timeout: 请求超时秒数
        retries: 重试次数

    Returns:
        requests.Session 实例（已配置代理和重试）
    """
    import requests as _req
    import logging
    _logger = logging.getLogger(__name__)

    session = _req.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    })

    # 代理（SOCKSProxyManager 不可用时自动降级到直连）
    proxies = get_proxies()
    if proxies:
        try:
            # 尝试验证代理可用性
            test_session = _req.Session()
            test_session.proxies.update(proxies)
            test_session.get("https://datacenter.eastmoney.com",
                              timeout=5, proxies=proxies)
            session.proxies.update(proxies)
            _logger.debug("[proxy] SOCKS proxy OK")
        except Exception as e:
            _logger.warning(f"[proxy] SOCKS代理不可用 ({e})，降级直连")
            # 降级到直连
            pass

    # 连接重试
    try:
        adapter = HTTPAdapter(max_retries=Retry(
            total=retries, connect=retries, backoff_factor=0.6,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET", "POST"],
        ))
        session.mount("https://", adapter)
        session.mount("http://", adapter)
    except Exception:
        pass  # 老版本urllib3兼容

    return session


# ═══════════════════════════════════════════════════════════════════════
# 限流共享网关 — 跨模块协调请求间隔
# ═══════════════════════════════════════════════════════════════════════

import time as _time
import random as _random

_EM_LAST_CALL = [0.0]
_DEFAULT_INTERVAL = 1.1  # 默认最低间隔（秒），避免触发东财>5 req/s封禁


def rate_limited_get(session: object, url: str, params: dict = None,
                     headers: dict = None, timeout: int = 15,
                     min_interval: float = _DEFAULT_INTERVAL) -> object:
    """
    限流HTTP GET请求 — 所有东财模块共用此函数，实现全局协调

    所有访问 push2.eastmoney.com / push2ex.eastmoney.com /
    emappdata.eastmoney.com / datacenter-web.eastmoney.com 的模块
    都应使用此函数，确保跨模块请求间隔 >= min_interval 秒。

    Args:
        session: requests.Session 实例
        url: 请求URL
        params: 查询参数
        headers: 请求头
        timeout: 超时秒数
        min_interval: 最小请求间隔（秒）

    Returns:
        requests.Response 对象
    """
    global _EM_LAST_CALL
    wait = min_interval - (_time.time() - _EM_LAST_CALL[0])
    if wait > 0:
        _time.sleep(wait + _random.uniform(0.05, 0.3))
    try:
        return session.get(url, params=params, headers=headers, timeout=timeout)
    finally:
        _EM_LAST_CALL[0] = _time.time()
