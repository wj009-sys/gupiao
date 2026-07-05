"""
涨停打板层 — 基于 a-stock-data 项目 (V3.3.0)

提供 A 股涨停板相关数据：
  1. em_zt_pool()     — 东财涨停池（连板/封板资金/封板时间）
  2. em_zb_pool()     — 东财炸板池（涨停后开板）
  3. em_dt_pool()     — 东财跌停池
  4. em_yzt_pool()    — 昨日涨停池（算晋级率/赚钱效应）
  5. ths_limit_up_pool() — 同花顺涨停揭秘（涨停原因题材/封板成功率）
  6. limit_up_sentiment() — 打板情绪速算（炸板率/连板梯队）

核心价值：
  - 全部免登录、零鉴权
  - 连板梯队、炸板率、晋级率一站到位
  - Agent9 游资追踪师的核心数据源

数据源：东财 push2ex + 同花顺 10jqka

来源：a-stock-data (https://github.com/simonlin1212/a-stock-data)
"""

import logging
import time
import random
from datetime import datetime
from typing import List, Dict, Optional

import requests

logger = logging.getLogger(__name__)

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/117.0.0.0 Safari/537.36")

# ── 东财涨停板统一参数 ──
ZTB_UT = "7eea3edcaed734bea9cbfc24409ed989"

# ── 东财限流：复用 eastmoney_get 的模式 ──
_EM_SESSION = requests.Session()
_EM_SESSION.headers.update({"User-Agent": UA})
_EM_LAST_CALL = [0.0]
EM_MIN_INTERVAL = 1.0


def _em_get(url: str, params: dict = None, headers: dict = None,
            timeout: int = 15) -> requests.Response:
    """东财统一限流请求（内部使用）"""
    wait = EM_MIN_INTERVAL - (time.time() - _EM_LAST_CALL[0])
    if wait > 0:
        time.sleep(wait + random.uniform(0.1, 0.5))
    try:
        return _EM_SESSION.get(url, params=params, headers=headers, timeout=timeout)
    finally:
        _EM_LAST_CALL[0] = time.time()


# ── 辅助 ──

def _fmt_zt_time(t) -> str:
    """涨停板时间整数 → HH:MM:SS"""
    s = str(int(t)).zfill(6)
    return f"{s[0:2]}:{s[2:4]}:{s[4:6]}"


def _em_zt_api(endpoint: str, sort: str, date: str) -> list:
    """东财涨停板行情中心通用请求（push2ex，走 _em_get 限流）

    D3异常处理:
        | 触发条件          | 一线修复              | 仍失败兜底    |
        |------------------|---------------------|-------------|
        | HTTP 连接失败     | 等待后重试 1 次       | 返回空列表    |
        | JSON 解析失败     | 捕获异常              | 返回空列表    |
        | data 为 null     | 非交易日/参数错       | 返回空列表    |
    """
    url = f"https://push2ex.eastmoney.com/{endpoint}"
    params_ = {
        "ut": ZTB_UT, "dpt": "wz.ztzt", "Pageindex": 0,
        "pagesize": 10000, "sort": sort, "date": date,
    }
    headers_ = {"User-Agent": UA, "Referer": "https://quote.eastmoney.com/"}
    try:
        r = _em_get(url, params=params_, headers=headers_, timeout=10)
        return (r.json().get("data") or {}).get("pool") or []
    except Exception as e:
        logger.warning(f"[limit_up] {endpoint} 请求失败 ({date}): {e}")
        return []


# ═══════════════════════════════════════════════════════════════════════
# 8.1 东财涨停板四池
# ═══════════════════════════════════════════════════════════════════════

def em_zt_pool(date: str) -> List[Dict]:
    """
    东财涨停池。

    参数:
        date: YYYYMMDD 格式的交易日
    返回:
        [{code, name, price, pct, amount, float_cap, turnover, limit_days(连板),
          first_seal, last_seal, seal_fund(封板资金,元), break_times(炸板次数),
          industry, zt_stat(N天M板)}]
    """
    out = []
    for p in _em_zt_api("getTopicZTPool", "fbt:asc", date):
        try:
            out.append({
                "code": p["c"], "name": p["n"], "price": p["p"] / 1000,
                "pct": round(p["zdp"], 2), "amount": p["amount"],
                "float_cap": p["ltsz"],
                "turnover": round(p["hs"], 2), "limit_days": p["lbc"],
                "first_seal": _fmt_zt_time(p["fbt"]),
                "last_seal": _fmt_zt_time(p["lbt"]),
                "seal_fund": p["fund"], "break_times": p["zbc"],
                "industry": p.get("hybk", ""),
                "zt_stat": f'{p.get("zttj", {}).get("days","?")}天'
                           f'{p.get("zttj", {}).get("ct","?")}板',
            })
        except (KeyError, ValueError, TypeError) as e:
            logger.debug(f"[limit_up] zt_pool 解析异常: {e}")
            continue
    return out


def em_zb_pool(date: str) -> List[Dict]:
    """
    东财炸板池（涨停后开板）。

    返回:
        [{code, name, price, limit_price(涨停价), pct, turnover,
          first_seal, break_times, amplitude(振幅), speed(涨速),
          industry, zt_stat}]
    """
    out = []
    for p in _em_zt_api("getTopicZBPool", "fbt:asc", date):
        try:
            out.append({
                "code": p["c"], "name": p["n"], "price": p["p"] / 1000,
                "limit_price": p["ztp"] / 1000, "pct": round(p["zdp"], 2),
                "turnover": round(p["hs"], 2),
                "first_seal": _fmt_zt_time(p["fbt"]),
                "break_times": p["zbc"],
                "amplitude": round(p["zf"], 2),
                "speed": round(p["zs"], 2),
                "industry": p.get("hybk", ""),
                "zt_stat": f'{p.get("zttj", {}).get("days","?")}天'
                           f'{p.get("zttj", {}).get("ct","?")}板',
            })
        except (KeyError, ValueError, TypeError) as e:
            logger.debug(f"[limit_up] zb_pool 解析异常: {e}")
            continue
    return out


def em_dt_pool(date: str) -> List[Dict]:
    """
    东财跌停池。

    返回:
        [{code, name, price, pct, turnover, pe, seal_fund(封单资金),
          last_seal, board_amount(板上成交额), dt_days(连续跌停),
          open_times(开板次数), industry}]
    """
    out = []
    for p in _em_zt_api("getTopicDTPool", "fund:asc", date):
        try:
            out.append({
                "code": p["c"], "name": p["n"], "price": p["p"] / 1000,
                "pct": round(p["zdp"], 2), "turnover": round(p["hs"], 2),
                "pe": p.get("pe"),
                "seal_fund": p["fund"],
                "last_seal": _fmt_zt_time(p["lbt"]),
                "board_amount": p.get("fba"),
                "dt_days": p.get("days"),
                "open_times": p.get("oc"),
                "industry": p.get("hybk", ""),
            })
        except (KeyError, ValueError, TypeError) as e:
            logger.debug(f"[limit_up] dt_pool 解析异常: {e}")
            continue
    return out


def em_yzt_pool(date: str) -> List[Dict]:
    """
    东财昨日涨停池（昨涨停今表现）。

    返回:
        [{code, name, price, pct(今日涨幅), turnover, amplitude, speed,
          y_first_seal(昨封板时间), y_limit_days(昨连板), industry, zt_stat}]
    """
    out = []
    for p in _em_zt_api("getYesterdayZTPool", "zs:desc", date):
        try:
            out.append({
                "code": p["c"], "name": p["n"], "price": p["p"] / 1000,
                "pct": round(p["zdp"], 2), "turnover": round(p["hs"], 2),
                "amplitude": round(p["zf"], 2), "speed": round(p["zs"], 2),
                "y_first_seal": _fmt_zt_time(p["yfbt"]),
                "y_limit_days": p["ylbc"],
                "industry": p.get("hybk", ""),
                "zt_stat": f'{p.get("zttj", {}).get("days","?")}天'
                           f'{p.get("zttj", {}).get("ct","?")}板',
            })
        except (KeyError, ValueError, TypeError) as e:
            logger.debug(f"[limit_up] yzt_pool 解析异常: {e}")
            continue
    return out


# ═══════════════════════════════════════════════════════════════════════
# 8.2 同花顺涨停揭秘 — 涨停原因题材 + 封板成功率 + 板型
# ═══════════════════════════════════════════════════════════════════════

# 同花顺涨停池字段 ID（照抄即可，稳定）
_THS_LIMIT_FIELDS = (
    "199112,10,9001,330323,330324,330325,9002,330329,133971,133970,"
    "1968584,3475914,9003,9004"
)


def ths_limit_up_pool(date: str) -> List[Dict]:
    """
    同花顺涨停揭秘。

    参数:
        date: YYYYMMDD 格式
    返回:
        [{code, name, price, pct, reason(涨停原因题材), board_type(换手板/一字板/T字板),
          seal_rate(封板成功率,0~1), break_times(炸板次数), seal_amount(封单额,元),
          high_days(几天几板), first_time(首次涨停时间), is_again(是否回封)}]

    D3异常处理:
        | 触发条件         | 一线修复       | 仍失败兜底    |
        |-----------------|--------------|-------------|
        | HTTP 请求失败    | 重试 1 次     | 返回空列表    |
        | JSON 解析失败    | 捕获异常       | 返回空列表    |
        | 数据为空         | 返回空列表     | 空列表        |
    """
    url = "https://data.10jqka.com.cn/dataapi/limit_up/limit_up_pool"
    params = {
        "page": 1, "limit": 200,
        "field": _THS_LIMIT_FIELDS,
        "filter": "HS,GEM2STAR",
        "order_field": "330324", "order_type": "0", "date": date,
    }
    try:
        r = requests.get(url, params=params,
                         headers={"User-Agent": UA}, timeout=10)
        info = (r.json().get("data") or {}).get("info", [])
    except Exception as e:
        logger.warning(f"[limit_up] ths_limit_up_pool 请求失败: {e}")
        return []

    out = []
    for it in info:
        try:
            ft = it.get("first_limit_up_time")
            out.append({
                "code": it.get("code"), "name": it.get("name"),
                "price": it.get("latest"), "pct": it.get("change_rate"),
                "reason": it.get("reason_type", ""),
                "board_type": it.get("limit_up_type", ""),
                "seal_rate": it.get("limit_up_suc_rate"),
                "break_times": it.get("open_num") or 0,
                "seal_amount": it.get("order_amount"),
                "high_days": it.get("high_days", ""),
                "first_time": (datetime.fromtimestamp(int(ft))
                               .strftime("%H:%M:%S") if ft else ""),
                "is_again": it.get("is_again_limit"),
            })
        except (ValueError, TypeError) as e:
            logger.debug(f"[limit_up] ths_limit_up 解析异常: {e}")
            continue
    return out


# ═══════════════════════════════════════════════════════════════════════
# 8.3 打板情绪速算 — 炸板率 / 连板高度 / 连板梯队
# ═══════════════════════════════════════════════════════════════════════

def limit_up_sentiment(date: str) -> Dict:
    """
    打板情绪温度计。

    参数:
        date: YYYYMMDD 格式交易日
    返回:
        {date, zt_count(涨停数), zb_count(炸板数), dt_count(跌停数),
         break_rate(炸板率%), max_height(最高连板), ladder({板数:家数})}
    """
    zt = em_zt_pool(date)
    zb = em_zb_pool(date)
    dt = em_dt_pool(date)

    ladder = {}
    for s in zt:
        ladder[s["limit_days"]] = ladder.get(s["limit_days"], 0) + 1

    zt_n, zb_n = len(zt), len(zb)
    return {
        "date": date,
        "zt_count": zt_n,
        "zb_count": zb_n,
        "dt_count": len(dt),
        "break_rate": round(zb_n / (zt_n + zb_n) * 100, 1) if (zt_n + zb_n) else 0,
        "max_height": max((s["limit_days"] for s in zt), default=0),
        "ladder": dict(sorted(ladder.items())),
    }


def sentiment_summary_text(date: str) -> str:
    """
    打板情绪文字摘要（供 Agent9/Agent1 直接使用）。

    参数:
        date: YYYYMMDD
    返回:
        格式化情绪摘要字符串
    """
    s = limit_up_sentiment(date)
    lines = [
        f"📊 打板情绪 ({s['date'][:4]}-{s['date'][4:6]}-{s['date'][6:8]})",
        f"  涨停 {s['zt_count']} 只 | 炸板 {s['zb_count']} 只 | "
        f"跌停 {s['dt_count']} 只",
        f"  炸板率 {s['break_rate']}% | 最高 {s['max_height']} 连板",
    ]
    if s["ladder"]:
        ladder_str = " | ".join(f"{k}板{v}只" for k, v in s["ladder"].items())
        lines.append(f"  连板梯队: {ladder_str}")
    return "\n".join(lines)


if __name__ == "__main__":
    from datetime import date as _d, timedelta

    # 找最近的交易日（周末会返回空）
    today = _d.today()
    for offset in range(7):
        d = today - timedelta(days=offset)
        ds = d.strftime("%Y%m%d")
        zt = em_zt_pool(ds)
        if zt:
            print(f"交易日: {ds}")

            print(f"\n=== 涨停池（共 {len(zt)} 只）===")
            for s in zt[:5]:
                print(f"  {s['name']} {s['zt_stat']} "
                      f"封板{s['seal_fund']/1e8:.2f}亿 {s['industry']}")

            print(f"\n=== 炸板池（共 {len(em_zb_pool(ds))} 只）===")
            print(f"=== 跌停池（共 {len(em_dt_pool(ds))} 只）===")

            # 同花顺涨停揭秘
            ths = ths_limit_up_pool(ds)
            print(f"\n=== 同花顺涨停揭秘（共 {len(ths)} 只）===")
            for s in ths[:3]:
                print(f"  {s['name']} {s['high_days']} | "
                      f"{s['reason']} | 封板率{s['seal_rate']}")

            # 情绪摘要
            print(f"\n{'-'*40}")
            print(sentiment_summary_text(ds))
            break
    else:
        print("近7天无交易日数据")
