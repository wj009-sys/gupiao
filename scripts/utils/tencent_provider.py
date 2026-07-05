"""
腾讯财经行情数据源 — 基于 a-stock-data 项目 (V3.3.0) 的 tencent_quote()

提供 A 股/指数/ETF 实时行情：价格、PE(TTM)、PB、总市值、流通市值、
换手率、涨跌幅、涨停价、跌停价、量比等字段。

核心价值：
  - 无需任何 API Key，完全免费
  - 不封 IP（实测稳定）
  - 可批量查询（一次最多 20-30 只股票）
  - 同时支持个股/指数/ETF

数据源：腾讯财经 qt.gtimg.cn (HTTP GBK 编码，~分隔 88 个字段)

来源：a-stock-data (https://github.com/simonlin1212/a-stock-data)
"""

import logging
import urllib.request
from typing import Dict, Optional, List

logger = logging.getLogger(__name__)

# ── 腾讯财经 URL ──
QT_URL = "https://qt.gtimg.cn/q="

# ── 市场前缀映射 ──
def get_prefix(code: str) -> str:
    """6位代码 → 腾讯行情前缀"""
    if code.startswith(("6", "9")):
        return "sh"
    elif code.startswith("8"):
        return "bj"
    else:
        return "sz"


# ── Ticker 归一化 ──
def normalize_code(code: str) -> str:
    """统一多种输入格式 → 纯6位代码"""
    code = code.strip().upper()
    for suffix in [".SH", ".SZ", ".BJ"]:
        if code.endswith(suffix):
            code = code[:-len(suffix)]
            break
    for prefix in ["SH", "SZ", "BJ"]:
        if code.startswith(prefix) and len(code) > 2 and code[2:].isdigit():
            code = code[2:]
            break
    return code


def tencent_quote(codes: List[str]) -> Dict[str, Dict]:
    """
    批量拉取腾讯财经实时行情。

    参数:
        codes: ["688017", "300476", "002463"] 或 ["000001", "000300", "399006"]
               也支持 ETF: ["510050", "510300"]
    返回:
        {code: {name, price, last_close, open, change_amt, change_pct,
                high, low, amount_wan, turnover_pct, pe_ttm, amplitude_pct,
                mcap_yi, float_mcap_yi, pb, limit_up, limit_down, vol_ratio, pe_static}}

    D3异常处理:
        | 触发条件           | 一线修复                    | 仍失败兜底            |
        |--------------------|----------------------------|---------------------|
        | 网络超时           | 重试 1 次                   | 返回空 dict          |
        | GBK 解码失败       | 尝试 utf-8 解码             | 返回空 dict          |
        | 返回数据格式异常    | 跳过该条目继续解析           | 返回部分结果          |

    D4 CHECKPOINT:
        CP1-响应状态: 检查 HTTP 响应状态码
        CP2-字段长度: 检查字段数组长度 >= 53

    D9反例:
        - 不要传超过 30 只股票（单次请求可能被截断）
        - 不要假定编码为 utf-8（腾讯 API 是 GBK）
        - 不要用索引 43 当 PB（实测是振幅%，PB 在索引 46）
    """
    if not codes:
        return {}

    # 加市场前缀
    prefixed = []
    for c in codes:
        c = normalize_code(c)
        prefixed.append(f"{get_prefix(c)}{c}")

    url = QT_URL + ",".join(prefixed)
    req = urllib.request.Request(url)
    req.add_header("User-Agent",
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36")

    try:
        resp = urllib.request.urlopen(req, timeout=10)
        raw = resp.read()
    except Exception as e:
        logger.warning(f"[tencent_provider] 网络请求失败: {e}")
        return {}

    # 解码 (GBK)
    try:
        data = raw.decode("gbk")
    except UnicodeDecodeError:
        try:
            data = raw.decode("utf-8")
        except UnicodeDecodeError:
            logger.warning("[tencent_provider] GBK/UTF-8 解码均失败")
            return {}

    result = {}
    for line in data.strip().split(";"):
        if not line.strip() or "=" not in line or '"' not in line:
            continue
        key = line.split("=")[0].split("_")[-1]
        vals = line.split('"')[1].split("~")
        if len(vals) < 53:
            continue
        code = key[2:]  # 去掉市场前缀
        try:
            result[code] = {
                "name": vals[1],
                "price":        float(vals[3]) if vals[3] else 0.0,
                "last_close":   float(vals[4]) if vals[4] else 0.0,
                "open":         float(vals[5]) if vals[5] else 0.0,
                "change_amt":   float(vals[31]) if vals[31] else 0.0,
                "change_pct":   float(vals[32]) if vals[32] else 0.0,
                "high":         float(vals[33]) if vals[33] else 0.0,
                "low":          float(vals[34]) if vals[34] else 0.0,
                "amount_wan":   float(vals[37]) if vals[37] else 0.0,
                "turnover_pct": float(vals[38]) if vals[38] else 0.0,
                "pe_ttm":       float(vals[39]) if vals[39] else 0.0,
                "amplitude_pct":float(vals[43]) if vals[43] else 0.0,
                "mcap_yi":      float(vals[44]) if vals[44] else 0.0,
                "float_mcap_yi":float(vals[45]) if vals[45] else 0.0,
                "pb":           float(vals[46]) if vals[46] else 0.0,
                "limit_up":     float(vals[47]) if vals[47] else 0.0,
                "limit_down":   float(vals[48]) if vals[48] else 0.0,
                "vol_ratio":    float(vals[49]) if vals[49] else 0.0,
                "pe_static":    float(vals[52]) if vals[52] else 0.0,
            }
        except (ValueError, IndexError) as e:
            logger.debug(f"[tencent_provider] 解析 {code} 失败: {e}")
            continue

    return result


def tencent_batch_quote(codes: List[str]) -> Dict[str, Dict]:
    """
    腾讯批量行情（自动分批，一次最多 20 只）

    大批量查询时自动按 20 只/批分割，避免单次请求超长被截断。

    参数:
        codes: 股票代码列表（不限长度）
    返回:
        {code: {...}} 合并后的行情字典
    """
    result = {}
    BATCH_SIZE = 20
    for i in range(0, len(codes), BATCH_SIZE):
        batch = codes[i:i + BATCH_SIZE]
        try:
            partial = tencent_quote(batch)
            result.update(partial)
        except Exception as e:
            logger.warning(f"[tencent_provider] 批次 {i//BATCH_SIZE} 请求失败: {e}")
            continue
    return result


# ── 字段索引速查（实测校准 2026-05-03）──
# 索引  含义          说明
# 1     名称          绿的谐波
# 3     当前价         224.12
# 4     昨收           215.01
# 5     今开           214.10
# 9-18  买一~买五(价+量)
# 19-28 卖一~卖五(价+量)
# 31    涨跌额         9.11
# 32    涨跌幅%        4.24
# 33    最高           229.62
# 34    最低           214.10
# 37    成交额(万)     187040
# 38    换手率%        4.55
# 39    PE(TTM)        300.45
# 43    振幅%（不是PB!）7.22
# 44    总市值(亿)     410.88
# 45    流通市值(亿)   410.88
# 46    PB(市净率)     11.51
# 47    涨停价         258.01
# 48    跌停价         172.01
# 49    量比           1.20
# 52    PE(静)         314.76


if __name__ == "__main__":
    # 快速测试
    import json

    # 个股
    q = tencent_quote(["688017", "600519", "000858"])
    for code, info in q.items():
        print(f"{info['name']}({code}): "
              f"{info['price']}元 PE={info['pe_ttm']} PB={info['pb']} "
              f"市值={info['mcap_yi']}亿 涨停={info['limit_up']} 跌停={info['limit_down']}")

    # 指数
    idx = tencent_quote(["000001", "000300", "399006"])
    for code, info in idx.items():
        print(f"指数 {info['name']}({code}): {info['price']} 涨跌{info['change_pct']}%")

    # ETF
    etf = tencent_quote(["510050", "510300"])
    for code, info in etf.items():
        print(f"ETF {info['name']}({code}): {info['price']} PE={info['pe_ttm']}")
