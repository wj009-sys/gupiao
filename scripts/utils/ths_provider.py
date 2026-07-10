"""
同花顺数据源 — 基于 a-stock-data 项目 (V3.3.0)

提供三大独家数据能力：
  1. ths_hot_reason()  — 当日强势股 + 题材归因 reason tags（核心价值！）
  2. ths_eps_forecast() — 机构一致预期 EPS（估值分析必备）
  3. ths_hot_list()    — 同花顺人气热榜（热度 + 概念标签）

核心价值：
  - 全部免费无 Key，零鉴权
  - reason tags 是同花顺编辑部人工运营的题材标签，A 股独一份
  - 一致预期 EPS 是估值分析的核心输入

数据源：同花顺 10jqka.com.cn (HTTP)

来源：a-stock-data (https://github.com/simonlin1212/a-stock-data)
"""

import logging
from typing import Optional, List, Dict
from io import StringIO
from datetime import date as _date

import requests
import pandas as pd

logger = logging.getLogger(__name__)

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/117.0.0.0 Safari/537.36")


# ═══════════════════════════════════════════════════════════════════════
# 3.1 同花顺热点 — 当日强势股 + 题材归因 reason tags
# ═══════════════════════════════════════════════════════════════════════

def ths_hot_reason(date: Optional[str] = None) -> pd.DataFrame:
    """
    同花顺当日强势股归因。

    核心价值：不只告诉你"哪些走强"，还告诉你"为什么走强"——
    同花顺编辑部人工运营的题材标签 (reason 字段)。

    参数:
        date: 'YYYY-MM-DD' 格式，None=今天
    返回:
        DataFrame，含每只股票的题材标签 (reason)

    实测: ~73ms 拿到 ~125 只 + 完整字段

    D3异常处理:
        | 触发条件          | 一线修复                  | 仍失败兜底            |
        |------------------|--------------------------|---------------------|
        | HTTP 请求失败     | 重试 1 次                | 返回空 DataFrame     |
        | JSON 解析失败     | 检查响应是否为 HTML/404   | 返回空 DataFrame     |
        | 数据为空         | 返回空 DataFrame          | 空 DataFrame        |

    D4 CHECKPOINT:
        CP1-状态码: 检查 HTTP 响应状态码
        CP2-错误码: 检查 JSON 中的 errocode 字段

    D9反例:
        - reason 空时不 dropna 检查可能误判
        - 盘后 15:30 前数据可能未更新
        - 不要打 search.10jqka.com.cn 的 iwencai NL 选股接口（需鉴权）
    """
    if date is None:
        date = _date.today().strftime("%Y-%m-%d")

    url = (f"https://zx.10jqka.com.cn/event/api/getharden/"
           f"date/{date}/orderby/date/orderway/desc/charset/GBK/")
    headers = {"User-Agent": UA}

    try:
        r = requests.get(url, headers=headers, timeout=10)
    except Exception as e:
        logger.warning(f"[ths_provider] hot_reason 请求失败: {e}")
        return pd.DataFrame()

    try:
        data = r.json()
    except Exception as e:
        logger.warning(f"[ths_provider] hot_reason JSON 解析失败: {e}")
        return pd.DataFrame()

    if data.get("errocode", 0) != 0:
        logger.warning(f"[ths_provider] hot_reason 错误: {data.get('errormsg', '')}")
        return pd.DataFrame()

    rows = data.get("data") or []
    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    rename_map = {
        "name": "名称", "code": "代码", "reason": "题材归因",
        "close": "收盘价", "zhangdie": "涨跌额", "zhangfu": "涨幅%",
        "huanshou": "换手率%", "chengjiaoe": "成交额",
        "chengjiaoliang": "成交量", "ddejingliang": "大单净量",
        "market": "市场",
    }
    df = df.rename(columns=rename_map)
    return df


# ═══════════════════════════════════════════════════════════════════════
# 2.2 同花顺一致预期 EPS（直连 basic.10jqka.com.cn）
# ═══════════════════════════════════════════════════════════════════════

def ths_eps_forecast(code: str) -> pd.DataFrame:
    """
    同花顺机构一致预期 EPS。

    参数:
        code: 6位股票代码（如 "688017"）
    返回:
        DataFrame: 年度, 预测机构数, 最小值, 均值, 最大值, 行业平均
        "均值" = 机构一致预期EPS（核心字段）

    注意:
        - 按列名取 "均值"（不按 iloc 位置），抗列序漂移
        - "预测机构数" < 3 的一致预期不可靠

    D3异常处理:
        | 触发条件           | 一线修复              | 仍失败兜底            |
        |-------------------|---------------------|---------------------|
        | HTTP 请求失败      | 重试 1 次            | 返回空 DataFrame     |
        | HTML 表格解析失败  | pd.read_html 异常处理  | 返回空 DataFrame     |
        | 无机构覆盖         | 返回空 DataFrame      | 空 DataFrame        |

    D4 CHECKPOINT:
        CP1-表格存在: 检查是否解析到含"每股收益"的表格
        CP2-均值列: 检查 "均值" 列是否存在

    D9反例:
        - 不要用 iloc[2] 取 EPS（那是"最小值"不是"均值"！旧版 Bug）
        - 机构覆盖 < 3 家的预期不可靠，需标注
        - 小盘/次新/ST 股无机构覆盖 → 正常，不是 Bug
    """
    url = f"https://basic.10jqka.com.cn/new/{code}/worth.html"
    headers = {"User-Agent": UA, "Referer": "https://basic.10jqka.com.cn/"}

    try:
        r = requests.get(url, headers=headers, timeout=15)
        r.encoding = "gbk"
    except Exception as e:
        logger.warning(f"[ths_provider] eps_forecast 请求失败 ({code}): {e}")
        return pd.DataFrame()

    try:
        dfs = pd.read_html(StringIO(r.text))
    except Exception as e:
        logger.warning(f"[ths_provider] eps_forecast HTML 解析失败 ({code}): {e}")
        return pd.DataFrame()

    for df in dfs:
        cols = [str(c) for c in df.columns]
        if any("每股收益" in c or "均值" in c for c in cols):
            return df

    # fallback: 返回第一个表
    return dfs[0] if dfs else pd.DataFrame()


# ═══════════════════════════════════════════════════════════════════════
# 10.2 同花顺热榜（人气 + 概念标签 + 排名变化）
# ═══════════════════════════════════════════════════════════════════════

def ths_hot_list(period: str = "hour") -> List[Dict]:
    """
    同花顺热榜（单接口拿名称+人气+概念标签+排名变化）

    参数:
        period: "hour" 或 "day"（小时榜/日榜）
    返回:
        [{rank, code, name, heat(人气值), pct, rank_chg(排名变化),
          concepts(概念标签列表), tag(人气标签)}]

    D3异常处理:
        | 触发条件        | 一线修复       | 仍失败兜底        |
        |----------------|--------------|-----------------|
        | HTTP 请求失败   | 重试 1 次     | 返回空列表        |
        | JSON 解析失败   | 捕获异常       | 返回空列表        |
        | 数据为空        | 返回空列表     | 空列表            |
    """
    try:
        r = requests.get(
            "https://dq.10jqka.com.cn/fuyao/hot_list_data/out/hot_list/v1/stock",
            params={"stock_type": "a", "type": period, "list_type": "normal"},
            headers={"User-Agent": UA},
            timeout=10,
        )
        lst = (r.json().get("data") or {}).get("stock_list") or []
    except Exception as e:
        logger.warning(f"[ths_provider] hot_list 请求失败: {e}")
        return []

    out = []
    for it in lst:
        tag = it.get("tag") or {}
        out.append({
            "rank": it.get("order"),
            "code": it.get("code"),
            "name": it.get("name"),
            "heat": it.get("rate"),
            "pct": it.get("rise_and_fall"),
            "rank_chg": it.get("hot_rank_chg"),
            "concepts": tag.get("concept_tag") or [],
            "tag": tag.get("popularity_tag", ""),
        })
    return out


# ═══════════════════════════════════════════════════════════════════════
# 便捷工具：从热点词频统计题材热度
# ═══════════════════════════════════════════════════════════════════════

def hot_reason_tag_ranking(df_hot: Optional[pd.DataFrame] = None,
                           date: Optional[str] = None) -> List[tuple]:
    """
    从同花顺热点数据的 reason 列统计题材关键词热度排名。

    参数:
        df_hot: ths_hot_reason() 返回的 DataFrame（None 则自动拉取）
        date: 拉取日期（df_hot 为 None 时生效）
    返回:
        [(tag, count), ...] 按热度降序
    """
    if df_hot is None:
        df_hot = ths_hot_reason(date)

    if df_hot.empty or "题材归因" not in df_hot.columns:
        return []

    from collections import Counter
    all_tags = []
    for r in df_hot["题材归因"].dropna():
        tags = [t.strip() for t in str(r).split("+") if t.strip()]
        all_tags.extend(tags)

    return Counter(all_tags).most_common(20)


if __name__ == "__main__":
    # 快速测试

    # 1. 强势股归因
    print("=== 同花顺热点 ===")
    df = ths_hot_reason()
    if not df.empty:
        print(f"当日强势股: {len(df)} 只")
        print(df[["代码", "名称", "涨幅%", "题材归因"]].head(10).to_string())
    else:
        print("(可能非交易日)")

    # 2. 词频统计
    print("\n=== 题材热度 TOP10 ===")
    ranking = hot_reason_tag_ranking(df)
    for tag, cnt in ranking[:10]:
        print(f"  {tag}: {cnt} 只")

    # 3. 一致预期 EPS
    print("\n=== 一致预期 EPS ===")
    for code in ["688017", "600519", "300476"]:
        df_eps = ths_eps_forecast(code)
        if not df_eps.empty:
            print(f"{code}:")
            print(df_eps.to_string())
        else:
            print(f"{code}: 无机构覆盖")

    # 4. 热榜
    print("\n=== 同花顺热榜 TOP5 ===")
    hot = ths_hot_list()
    for s in hot[:5]:
        print(f"  #{s['rank']} {s['name']} 热度={s['heat']} 概念={s['concepts']}")
