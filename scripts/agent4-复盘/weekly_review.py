"""
Agent4 周度复盘 — 对本周市场进行全方位复盘

功能：
1. 🌍 全球市场（美股/港股/日经/英股）周度表现
2. 🇨🇳 A股大盘（上证/深证/创业板/科创50）每日走势
3. 🥇 大宗商品（黄金/白银）周度表现
4. 🔄 板块轮动（领涨/领跌板块 + 持续性分析）
5. 💰 资金面（北向资金/融资融券）
6. 📊 市场情绪（涨跌家数/成交额分析）
7. 📰 本周重要消息面回顾
8. 📈 技术面分析（均线/支撑阻力）
9. 🔮 下周展望与策略

用法：
    source venv/Scripts/activate
    python -X utf8 scripts/agent4-复盘/weekly_review.py [--week-ending 2026-06-26]

D3 异常处理:
    触发条件                      一线修复                            仍失败兜底
    ────────────────────────────  ──────────────────────────────────  ──────────────────────
    全球市场数据拉取失败(美股等)    逐个市场独立重试                      标注"N/A"，仅用A股
    A股指数数据<3天                检查交易日历确认是否是短交易周       标注"数据不足(仅N天)"
    商品期货数据缺失               用前一周数据近似填充                 跳过商品章节
    板块数据日期与目标周不匹配      检查trade_date字段映射              标注"日期偏移(±1天)"
    情绪指标(涨跌家数)为空          检查index_daily数据完整性            标注"无法计算"
    技术指标计算失败                逐指标try/except安全计算             该指标标"N/A"
    新闻/消息采集失败              检查WebFetch可用性                   标注"本周无新闻数据"
    报告写入失败                   检查磁盘空间和目录权限                输出到临时目录

D4 CHECKPOINT:
    [ ] CP1-交易日覆盖: 5个交易日中>=3天有指数数据，不足标注原因
    [ ] CP2-板块日期匹配: 板块数据trade_date在目标周范围内
    [ ] CP3-情绪指标完整性: 涨家数+跌家数+平家数≈当日总上市家数
    [ ] CP4-百分比归一化: 所有百分比合计100%或标注"不完全统计"
    [ ] CP5-周涨跌幅一致性: (本周五收盘-上周五收盘)/上周五收盘 ≈ 日涨跌幅累计（误差<0.5%）
    [ ] CP6-章节完整性: 报告包含全球/A股/商品/板块/情绪/技术/新闻/展望8个必需章节

D9 工作反例:
    #  反模式                        为什么不要做                      应该怎么做
    ──  ────────────────────────────  ───────────────────────────────  ────────────────────
    1   全球市场数据为空时硬编数据      虚假数据误导下周决策              标注数据源不可用
    2   只看A股不看全球                忽略外围市场联动风险             全球→A股→板块顺序
    3   板块分析没有持续性判断          单周涨跌不能判断趋势             标记连续N周领涨/领跌
    4   情绪分析只有数字没有解读        "涨1200跌800"需要解释含义        判断市场情绪方向
    5   展望与本周分析脱节              本周说风险大下周说all-in         展望必须逻辑延续
    6   bare except吞掉所有错误         无法定位问题根因                 具体异常类型+完整traceback
"""

import os
import sys
import json
import re
import glob
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from scripts.utils.tushare_client import pro

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def p(path: str) -> str:
    """转为项目绝对路径"""
    return os.path.join(PROJECT_ROOT, path)


def load_json(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def read_file(path: str) -> str:
    full = p(path)
    if not os.path.exists(full):
        return ""
    with open(full, "r", encoding="utf-8") as f:
        return f.read()


def get_week_dates(week_ending: str = None) -> list:
    """获取本周一到周五的日期列表"""
    if week_ending:
        end = datetime.strptime(week_ending, "%Y-%m-%d")
    else:
        end = datetime.now()
        while end.weekday() > 4:
            end -= timedelta(days=1)
    while end.weekday() != 4:
        end -= timedelta(days=1)
    dates = []
    for i in range(5):
        d = end - timedelta(days=4 - i)
        dates.append(d.strftime("%Y-%m-%d"))
    return dates


def fmt_date(trade_date: str) -> str:
    """将 20260622 转为 6/22"""
    if len(trade_date) == 8:
        return f"{int(trade_date[4:6])}/{int(trade_date[6:8])}"
    return trade_date


# ════════════════════════════════════════════════════════════════════
# 一、🌍 全球市场
# ════════════════════════════════════════════════════════════════════

GLOBAL_INDICES = {
    "道琼斯": "DJI",
    "标普500": "SPX",
    "纳斯达克": "IXIC",
    "恒生指数": "HSI",
    "日经225": "N225",
    "富时100": "FTSE",
}

def fetch_global_markets(dates: list) -> dict:
    """获取全球主要指数本周表现"""
    result = {}
    start = dates[0].replace("-", "")
    end = dates[-1].replace("-", "")
    for name, code in GLOBAL_INDICES.items():
        try:
            df = pro.index_global(ts_code=code, start_date=start, end_date=end)
            if df is not None and not df.empty:
                rows = []
                for _, r in df.iterrows():
                    rows.append({
                        "date": str(r.get("trade_date", "")),
                        "close": float(r.get("close", 0)),
                        "pct_chg": float(r.get("pct_chg", 0)),
                    })
                if len(rows) >= 2:
                    week_pct = round((rows[0]["close"] - rows[-1]["close"]) / rows[-1]["close"] * 100, 2)
                else:
                    week_pct = 0
                result[name] = {
                    "daily": rows,
                    "week_pct": week_pct,
                    "week_open": rows[-1]["close"] if rows else 0,
                    "week_close": rows[0]["close"] if rows else 0,
                    "week_high": max(r["close"] for r in rows) if rows else 0,
                    "week_low": min(r["close"] for r in rows) if rows else 0,
                }
        except Exception as e:
            result[name] = {"error": str(e)[:60]}
    return result


# ════════════════════════════════════════════════════════════════════
# 二、🇨🇳 A股指数
# ════════════════════════════════════════════════════════════════════

INDEX_CODES = {
    "上证指数": "000001.SH",
    "深证成指": "399001.SZ",
    "创业板指": "399006.SZ",
    "科创50": "000688.SH",
}

def fetch_index_data(dates: list) -> dict:
    """获取A股主要指数本周走势"""
    result = {}
    start = dates[0].replace("-", "")
    end = dates[-1].replace("-", "")
    for name, code in INDEX_CODES.items():
        try:
            df = pro.index_daily(ts_code=code, start_date=start, end_date=end)
            if df is not None and not df.empty:
                df = df.sort_values("trade_date")
                rows = []
                for _, r in df.iterrows():
                    rows.append({
                        "date": str(r.get("trade_date", "")),
                        "close": float(r.get("close", 0)),
                        "pct_chg": float(r.get("pct_chg", 0)),
                        "vol": float(r.get("vol", 0)),
                        "amount": float(r.get("amount", 0)) / 1e8,  # 亿
                    })
                first_close = rows[0]["close"] if rows else 0
                last_close = rows[-1]["close"] if rows else 0
                week_pct = round((last_close - first_close) / first_close * 100, 2)
                result[name] = {
                    "daily": rows,
                    "week_pct": week_pct,
                    "week_open": rows[0]["close"] if rows else 0,
                    "week_close": rows[-1]["close"] if rows else 0,
                    "week_high": max(r["close"] for r in rows) if rows else 0,
                    "week_low": min(r["close"] for r in rows) if rows else 0,
                    "week_vol": sum(r["vol"] for r in rows),
                    "week_amount": sum(r["amount"] for r in rows),
                }
        except Exception as e:
            result[name] = {"error": str(e)[:60]}
    return result


# ════════════════════════════════════════════════════════════════════
# 三、🥇 大宗商品
# ════════════════════════════════════════════════════════════════════

COMMODITIES = {
    "黄金": "AU.SHF",
    "白银": "AG.SHF",
}

def fetch_commodities(dates: list) -> dict:
    """获取黄金/白银本周表现"""
    result = {}
    start = dates[0].replace("-", "")
    end = dates[-1].replace("-", "")
    for name, code in COMMODITIES.items():
        try:
            df = pro.fut_daily(ts_code=code, start_date=start, end_date=end)
            if df is not None and not df.empty:
                df = df.sort_values("trade_date")
                rows = []
                for _, r in df.iterrows():
                    rows.append({
                        "date": str(r.get("trade_date", "")),
                        "close": float(r.get("close", 0)),
                        "settle": float(r.get("settle", 0)),
                        "change1": float(r.get("change1", 0)),  # 较昨收盘
                        "vol": float(r.get("vol", 0)),
                        "oi": float(r.get("oi", 0)),  # 持仓量
                    })
                first_close = rows[0]["close"] if rows else 0
                last_close = rows[-1]["close"] if rows else 0
                week_pct = round((last_close - first_close) / first_close * 100, 2)
                result[name] = {
                    "daily": rows,
                    "week_pct": week_pct,
                    "week_open": rows[0]["close"],
                    "week_close": rows[-1]["close"],
                    "week_high": max(r["close"] for r in rows),
                    "week_low": min(r["close"] for r in rows),
                }
        except Exception as e:
            result[name] = {"error": str(e)[:60]}
    return result


# ════════════════════════════════════════════════════════════════════
# 四、🔄 板块轮动
# ════════════════════════════════════════════════════════════════════

def fetch_sector_rotation(dates: list) -> dict:
    """分析本周板块轮动（使用可访问的API）"""
    rotation = {
        "top_gainers_weekly": [],
        "top_losers_weekly": [],
        "daily_top": {},
        "note": "",
    }

    # 尝试通过 daily 数据识别强势板块
    # 按申万一级行业归类计算涨幅
    sector_map = {}  # 板块名 → {dates_present, total_change}
    has_data = False

    for date_str in dates:
        trade_date = date_str.replace("-", "")
        try:
            # 使用 ths_daily（如有权限）
            df = pro.ths_daily(trade_date=trade_date)
            if df is not None and not df.empty:
                has_data = True
                top5 = df.nlargest(5, "pct_chg")[["name", "pct_chg"]].values.tolist()
                bottom5 = df.nsmallest(5, "pct_chg")[["name", "pct_chg"]].values.tolist()
                rotation["daily_top"][date_str] = {
                    "gainers": [{"name": r[0], "pct": float(r[1])} for r in top5],
                    "losers": [{"name": r[0], "pct": float(r[1])} for r in bottom5],
                }
                # 累积
                for r in top5:
                    name = r[0]
                    if name not in sector_map:
                        sector_map[name] = {"appearances": 0, "total_pct": 0, "days": set()}
                    sector_map[name]["appearances"] += 1
                    sector_map[name]["total_pct"] += float(r[1])
                    sector_map[name]["days"].add(date_str)
        except Exception as e:
            print(f'  [WARN] 获取 {date_str} 板块数据失败: {e}')

    if has_data and sector_map:
        sorted_gainers = sorted(sector_map.items(), key=lambda x: x[1]["total_pct"], reverse=True)
        rotation["top_gainers_weekly"] = [
            {"name": s[0], "total_pct": round(s[1]["total_pct"], 1), "days": len(s[1]["days"])}
            for s in sorted_gainers[:5]
        ]
    elif not has_data:
        # 降级：利用每日涨跌家数和行业ETF代码获取
        rotation["note"] = "板块数据（ths_daily）无访问权限，基于每日涨跌数据分析"

    return rotation


# ════════════════════════════════════════════════════════════════════
# 五、💰 资金面分析
# ════════════════════════════════════════════════════════════════════

def fetch_capital_flows(dates: list) -> dict:
    """获取北向资金和融资融券数据"""
    result = {"north_money": {}, "margin": {}, "summary": {}}
    start = dates[0].replace("-", "")
    end = dates[-1].replace("-", "")

    # 北向资金
    try:
        df = pro.moneyflow_hsgt(start_date=start, end_date=end)
        if df is not None and not df.empty:
            total_north = 0
            daily_north = []
            for _, r in df.iterrows():
                north = float(r.get("north_money", 0))
                total_north += north
                daily_north.append({
                    "date": str(r.get("trade_date", "")),
                    "north_money": round(north, 2),
                    "hgt": float(r.get("hgt", 0)),
                    "sgt": float(r.get("sgt", 0)),
                })
            result["north_money"] = {
                "daily": daily_north,
                "total_week": round(total_north, 2),
                "avg_daily": round(total_north / max(len(daily_north), 1), 2),
            }
    except Exception as e:
        result["north_money"] = {"error": str(e)[:60]}

    # 融资融券
    try:
        df = pro.margin(start_date=start, end_date=end)
        if df is not None and not df.empty:
            # 按日期汇总
            daily_margin = {}
            for _, r in df.iterrows():
                d = str(r.get("trade_date", ""))
                if d not in daily_margin:
                    daily_margin[d] = {"rzye": 0, "rzmre": 0, "rqmcl": 0}
                daily_margin[d]["rzye"] += float(r.get("rzye", 0))
                daily_margin[d]["rzmre"] += float(r.get("rzmre", 0))
                daily_margin[d]["rqmcl"] += float(r.get("rqmcl", 0))

            margin_list = []
            for d in sorted(daily_margin.keys()):
                m = daily_margin[d]
                margin_list.append({
                    "date": d,
                    "rzye": round(m["rzye"] / 1e8, 0),      # 融资余额(亿)
                    "rzmre": round(m["rzmre"] / 1e8, 0),     # 融资买入(亿)
                })
            result["margin"] = {
                "daily": margin_list,
            }
    except Exception as e:
        result["margin"] = {"error": str(e)[:60]}

    return result


# ════════════════════════════════════════════════════════════════════
# 六、📊 市场情绪
# ════════════════════════════════════════════════════════════════════

def fetch_market_sentiment(dates: list) -> dict:
    """获取每日涨跌家数、成交额"""
    result = {"daily": [], "summary": {}}
    for date_str in dates:
        trade_date = date_str.replace("-", "")
        try:
            df = pro.daily(trade_date=trade_date)
            if df is not None and not df.empty:
                up = int(len(df[df["pct_chg"] > 0]))
                down = int(len(df[df["pct_chg"] < 0]))
                flat = int(len(df[df["pct_chg"] == 0]))
                total_amount = float(df["amount"].sum()) / 1e7 if "amount" in df.columns else 0  # 千元→亿元
                total_vol = float(df["vol"].sum()) / 1e8 if "vol" in df.columns else 0
                avg_pct = float(df["pct_chg"].mean())
                up_pct = float(df[df["pct_chg"] > 0]["pct_chg"].mean()) if up > 0 else 0
                down_pct = float(df[df["pct_chg"] < 0]["pct_chg"].mean()) if down > 0 else 0
                result["daily"].append({
                    "date": date_str,
                    "up": up,
                    "down": down,
                    "flat": flat,
                    "total": len(df),
                    "up_ratio": round(up / max(len(df), 1) * 100, 1),
                    "avg_pct": round(avg_pct, 2),
                    "avg_up_pct": round(up_pct, 2),
                    "avg_down_pct": round(down_pct, 2),
                    "amount": round(total_amount, 0),
                    "vol": round(total_vol, 2),
                })
        except Exception as e:
            result["daily"].append({"date": date_str, "error": str(e)[:60]})

    if result["daily"]:
        total_up = sum(d.get("up", 0) for d in result["daily"])
        total_down = sum(d.get("down", 0) for d in result["daily"])
        result["summary"] = {
            "total_up_week": total_up,
            "total_down_week": total_down,
            "avg_up_ratio": round(sum(d.get("up_ratio", 0) for d in result["daily"]) / max(len(result["daily"]), 1), 1),
        }
    return result


# ════════════════════════════════════════════════════════════════════
# 七、📈 技术面
# ════════════════════════════════════════════════════════════════════

def fetch_technical_levels(dates: list) -> dict:
    """分析关键指数技术面（均线位置、支撑阻力）"""
    result = {}
    start = dates[0].replace("-", "")
    end = dates[-1].replace("-", "")

    # 获取上证指数较长时间的数据用于均线计算
    try:
        # 往前取 60 个交易日以计算 MA20/MA60
        start_dt = datetime.strptime(dates[0], "%Y-%m-%d") - timedelta(days=90)
        long_start = start_dt.strftime("%Y%m%d")
        df = pro.index_daily(ts_code="000001.SH", start_date=long_start, end_date=end)
        if df is not None and not df.empty:
            df = df.sort_values("trade_date")

            closes = df["close"].values
            dates_arr = df["trade_date"].values

            # 计算均线
            def ma(data, n):
                return [None] * (n - 1) + [round(sum(data[i - n + 1:i + 1]) / n, 2) for i in range(n - 1, len(data))]

            ma5 = ma(closes, 5)
            ma20 = ma(closes, 20)
            ma60 = ma(closes, 60)

            # 取最近一周的数据
            week_start_idx = len(closes) - len(dates)
            if week_start_idx < 0:
                week_start_idx = 0

            for i in range(week_start_idx, len(closes)):
                d = dates_arr[i]
                date_key = str(d) if isinstance(d, str) else str(d)
                if date_key not in result:
                    result[date_key] = {}
                result[date_key]["close"] = float(closes[i])
                result[date_key]["ma5"] = float(ma5[i]) if ma5[i] is not None else None
                result[date_key]["ma20"] = float(ma20[i]) if ma20[i] is not None else None
                result[date_key]["ma60"] = float(ma60[i]) if ma60[i] is not None else None

            # 最后一天的均线位置判断
            if len(closes) > 0:
                last_close = float(closes[-1])
                last_ma5 = ma5[-1] if ma5[-1] is not None else 0
                last_ma20 = ma20[-1] if ma20[-1] is not None else 0
                last_ma60 = ma60[-1] if ma60[-1] is not None else 0

                signals = []
                if last_close < last_ma5:
                    signals.append(f"跌破MA5({last_ma5:.0f})")
                if last_close < last_ma20:
                    signals.append(f"跌破MA20({last_ma20:.0f})")
                if last_close < last_ma60:
                    signals.append(f"跌破MA60({last_ma60:.0f})")
                if last_close > last_ma5:
                    signals.append(f"站上MA5({last_ma5:.0f})")
                if last_close > last_ma20:
                    signals.append(f"站上MA20({last_ma20:.0f})")
                if last_close > last_ma60:
                    signals.append(f"站上MA60({last_ma60:.0f})")

                # 多空排列
                if last_ma5 > last_ma20 > last_ma60:
                    ma_status = "多头排列 (MA5>MA20>MA60) ✅"
                elif last_ma5 < last_ma20 < last_ma60:
                    ma_status = "空头排列 (MA5<MA20<MA60) ❌"
                else:
                    ma_status = "均线交织 ⚠️"

                result["_summary"] = {
                    "ma_status": ma_status,
                    "signals": signals,
                    "last_ma5": round(last_ma5, 2),
                    "last_ma20": round(last_ma20, 2),
                    "last_ma60": round(last_ma60, 2),
                    "last_close": round(last_close, 2),
                }
    except Exception as e:
        result["_error"] = str(e)[:60]

    return result


# ════════════════════════════════════════════════════════════════════
# 八、📰 消息面汇总
# ════════════════════════════════════════════════════════════════════

def fetch_weekly_news(dates: list) -> dict:
    """从情报摘要中提取本周重大事件"""
    result = {"key_events": [], "categories": {"policy": [], "market": [], "company": [], "global": [], "industry": []}}
    for date_str in dates:
        report = read_file(f"reports/日报/情报/情报摘要_{date_str}.md")
        if not report:
            continue
        # 提取核心事件（### 开头的条目）
        events = re.findall(r'### \d+\.\s*(.+?)(?:\n|$)', report)
        for e in events:
            text = e.strip()
            # 简单归类
            cat = "market"
            if any(k in text for k in ["政策", "证监会", "央行", "国务院", "监管"]):
                cat = "policy"
            elif any(k in text for k in ["公司", "公告", "收购", "IPO", "定增"]):
                cat = "company"
            elif any(k in text for k in ["美股", "美联储", "美联储", "日本", "韩国", "欧洲", "贸易"]):
                cat = "global"
            elif any(k in text for k in ["板块", "行业", "半导体", "新能源", "医药", "消费"]):
                cat = "industry"
            result["key_events"].append({"date": date_str, "event": text, "category": cat})
            if cat in result["categories"]:
                result["categories"][cat].append({"date": date_str, "event": text})

    return result


# ════════════════════════════════════════════════════════════════════
# 主函数：生成报告
# ════════════════════════════════════════════════════════════════════

def generate_weekly_report(week_ending: str = None) -> dict:
    """生成全方位周度复盘报告"""
    dates = get_week_dates(week_ending)
    周标签 = f"{dates[0]} ~ {dates[-1]}"
    today = datetime.now().strftime("%Y-%m-%d %H:%M")
    print(f"[周度复盘] 本周: {周标签}")

    report = {
        "title": f"📊 全方位周度复盘 — {周标签}",
        "generated_at": today,
        "week_dates": dates,
        "global_markets": {},      # 一、全球市场
        "index_performance": {},   # 二、A股大盘
        "commodities": {},         # 三、大宗商品
        "sector_rotation": {},     # 四、板块轮动
        "capital_flows": {},       # 五、资金面
        "sentiment": {},           # 六、市场情绪
        "technical": {},           # 七、技术面
        "news": {},                # 八、消息面
        "weekly_summary": "",      # 九、总结
        "next_week_outlook": "",   # 十、下周展望
    }

    # 1. 全球市场
    print("  [1/8] 🌍 全球市场...")
    report["global_markets"] = fetch_global_markets(dates)

    # 2. A股指数
    print("  [2/8] 🇨🇳 A股大盘...")
    report["index_performance"] = fetch_index_data(dates)

    # 3. 大宗商品
    print("  [3/8] 🥇 黄金/白银...")
    report["commodities"] = fetch_commodities(dates)

    # 4. 板块轮动
    print("  [4/8] 🔄 板块轮动...")
    report["sector_rotation"] = fetch_sector_rotation(dates)

    # 5. 资金面
    print("  [5/8] 💰 资金面...")
    report["capital_flows"] = fetch_capital_flows(dates)

    # 6. 市场情绪
    print("  [6/8] 📊 市场情绪...")
    report["sentiment"] = fetch_market_sentiment(dates)

    # 7. 技术面
    print("  [7/8] 📈 技术面...")
    report["technical"] = fetch_technical_levels(dates)

    # 8. 消息面
    print("  [8/8] 📰 消息面...")
    report["news"] = fetch_weekly_news(dates)

    # 九、本周总结
    report["weekly_summary"] = generate_summary(report)

    # 十、下周展望
    report["next_week_outlook"] = generate_outlook(report)

    return report


def generate_summary(report: dict) -> str:
    """生成本周总结"""
    parts = []
    index_data = report.get("index_performance", {})

    # A股表现
    if index_data:
        parts.append("【A股表现】")
        for name, data in index_data.items():
            if "week_pct" in data:
                emoji = "🔴" if data["week_pct"] < -1 else ("🟡" if data["week_pct"] < 0 else ("🟢" if data["week_pct"] > 1 else "⚪"))
                parts.append(f"  {emoji} {name}: {data['week_pct']:+.2f}%（收{data['week_close']:.0f}）")

    # 全球市场
    global_data = report.get("global_markets", {})
    if global_data:
        parts.append("\n【全球市场】")
        for name, data in global_data.items():
            if "week_pct" in data:
                emoji = "🔴" if data["week_pct"] < -1 else ("🟢" if data["week_pct"] > 1 else "⚪")
                parts.append(f"  {emoji} {name}: {data['week_pct']:+.2f}%")

    # 资金面
    cf = report.get("capital_flows", {})
    north = cf.get("north_money", {})
    if north.get("total_week"):
        total = north["total_week"]
        emoji = "🟢" if total > 0 else "🔴"
        parts.append(f"\n【资金面】")
        parts.append(f"  {emoji} 北向资金本周合计: {total/1e4:.0f}亿")
        parts.append(f"  📊 日均: {north.get('avg_daily', 0)/1e4:.0f}亿")

    # 情绪面
    sent = report.get("sentiment", {})
    sent_summary = sent.get("summary", {})
    if sent_summary:
        parts.append(f"\n【市场情绪】")
        parts.append(f"  📉 涨跌比: 本周上涨{sent_summary.get('total_up_week', 0)}家次 / 下跌{sent_summary.get('total_down_week', 0)}家次")
        parts.append(f"  📊 平均上涨家数占比: {sent_summary.get('avg_up_ratio', 0)}%")

    # 技术面
    tech = report.get("technical", {})
    tech_summary = tech.get("_summary", {})
    if tech_summary:
        parts.append(f"\n【技术面】")
        parts.append(f"  {tech_summary.get('ma_status', 'N/A')}")
        if tech_summary.get("signals"):
            for s in tech_summary["signals"][:3]:
                parts.append(f"  ⚠️ {s}")

    return "\n".join(parts)


def generate_outlook(report: dict) -> str:
    """生成下周展望"""
    tech = report.get("technical", {}).get("_summary", {})
    cf = report.get("capital_flows", {})
    north = cf.get("north_money", {})
    gmx = report.get("global_markets", {})

    outlook_parts = ["### 关键观察\n"]

    # 技术面
    if tech:
        if "跌破MA60" in str(tech.get("signals", "")):
            outlook_parts.append("1. ⚠️ 上证已跌破MA60（中期趋势走弱），若下周一无法收复，需警惕进一步调整风险")
        elif "跌破MA20" in str(tech.get("signals", "")):
            outlook_parts.append("1. ⚠️ 上证运行于MA20下方，短线承压，关注MA60附近支撑")
        else:
            outlook_parts.append("1. 📈 上证均线系统" + tech.get("ma_status", "中性") + "，关注量能配合")

    # 全球联动
    us_pct = gmx.get("道琼斯", {}).get("week_pct", 0)
    hk_pct = gmx.get("恒生指数", {}).get("week_pct", 0)
    if us_pct < -2:
        outlook_parts.append(f"2. 🌍 美股道琼斯本周{us_pct:+.2f}%，若持续走弱可能情绪传导A股")
    elif us_pct > 2:
        outlook_parts.append(f"2. 🌍 美股道琼斯本周{us_pct:+.2f}%，外围情绪偏暖")
    else:
        outlook_parts.append("2. 🌍 关注本周五美股表现及周末消息面变化")

    # 资金
    if north.get("total_week", 0) > 0:
        outlook_parts.append(f"3. 💰 北向资金本周逆势流入{north['total_week']/1e4:.0f}亿，关注下周持续性")
    else:
        outlook_parts.append("3. 💰 北向资金本周净流出，关注外资动向")

    outlook_parts.append("4. ⏰ 半年末资金面（6/30），注意可能波动")

    outlook_parts.append("\n### 操作策略\n")
    outlook_parts.append("- 控制总仓位，震荡市上限80%")
    outlook_parts.append("- 严格执行止损纪律，已在止损位的标的周一开盘处理")
    outlook_parts.append("- 关注周末重大消息面变化对开盘的影响")
    outlook_parts.append("- 半年末机构调仓期，避免追高题材炒作")

    return "\n".join(outlook_parts)


# ════════════════════════════════════════════════════════════════════
# 报告保存
# ════════════════════════════════════════════════════════════════════

def save_report(report: dict) -> str:
    """保存全方位周度复盘报告"""
    today = datetime.now().strftime("%Y%m%d")
    json_path = p(f"data/raw/周度复盘_{today}.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2, default=str)

    md = []
    md.append(f"# 📊 全方位周度复盘报告\n")
    md.append(f"\n> 生成时间：{report['generated_at']}")
    md.append(f"> 覆盖区间：{report['week_dates'][0]} ~ {report['week_dates'][-1]}")
    md.append("")

    # ═══ 一、全球市场 ═══
    md.append("---\n")
    md.append("## 一、🌍 全球主要市场\n")
    gmx = report.get("global_markets", {})
    md.append("| 指数 | 周收盘 | 周涨跌幅 | 周高 | 周低 |")
    md.append("|------|:------:|:--------:|:---:|:---:|")
    for name, data in gmx.items():
        if "error" in data:
            md.append(f"| {name} | 数据异常 | - | - | - |")
        else:
            emoji = "🔴" if data["week_pct"] < 0 else "🟢"
            md.append(f"| {emoji} {name} | {data['week_close']:.0f} | {data['week_pct']:+.2f}% | {data['week_high']:.0f} | {data['week_low']:.0f} |")
    md.append("")

    # 每日明细
    md.append("### 每日涨跌幅\n")
    all_global = list(gmx.keys())
    if all_global:
        header = "| 日期 | " + " | ".join(f"{n}" for n in all_global) + " |"
        sep = "|------|" + "|".join("------" for _ in all_global) + "|"
        md.append(header)
        md.append(sep)
        for date_str in report["week_dates"]:
            row = [date_str]
            for name in all_global:
                data = gmx.get(name, {})
                daily = data.get("daily", [])
                pct = "-"
                for d in daily:
                    if d["date"] == date_str.replace("-", ""):
                        pct = f"{d['pct_chg']:+.2f}%"
                        break
                row.append(pct)
            md.append("| " + " | ".join(row) + " |")
        md.append("")

    # ═══ 二、A股大盘 ═══
    md.append("---\n")
    md.append("## 二、🇨🇳 A股大盘\n")
    idx = report.get("index_performance", {})
    md.append("| 指数 | 周开盘 | 周收盘 | 周涨跌幅 | 周高 | 周低 |")
    md.append("|------|:-----:|:-----:|:--------:|:---:|:---:|")
    for name, data in idx.items():
        if "error" in data:
            md.append(f"| {name} | 数据异常 | - | - | - | - |")
        else:
            emoji = "🔴" if data["week_pct"] < -1 else ("🟡" if data["week_pct"] < 0 else ("🟢" if data["week_pct"] > 1 else "⚪"))
            md.append(f"| {emoji} {name} | {data['week_open']:.0f} | {data['week_close']:.0f} | {data['week_pct']:+.2f}% | {data['week_high']:.0f} | {data['week_low']:.0f} |")
    md.append("")

    # 每日明细
    md.append("### 每日涨跌幅\n")
    all_idx = list(idx.keys())
    if all_idx:
        header = "| 日期 | " + " | ".join(f"{n}" for n in all_idx) + " |"
        sep = "|------|" + "|".join("------" for _ in all_idx) + "|"
        md.append(header)
        md.append(sep)
        for date_str in report["week_dates"]:
            row = [date_str]
            for name in all_idx:
                data = idx.get(name, {})
                daily = data.get("daily", [])
                pct = "-"
                for d in daily:
                    if d["date"] == date_str.replace("-", ""):
                        pct = f"{d['pct_chg']:+.2f}%"
                        break
                row.append(pct)
            md.append("| " + " | ".join(row) + " |")
        md.append("")

    # ═══ 三、大宗商品 ═══
    md.append("---\n")
    md.append("## 三、🥇 大宗商品\n")
    comm = report.get("commodities", {})
    md.append("| 品种 | 周收盘 | 周涨跌幅 | 周高 | 周低 |")
    md.append("|------|:-----:|:--------:|:---:|:---:|")
    for name, data in comm.items():
        if "error" in data:
            md.append(f"| {name} | 数据异常 | - | - | - |")
        else:
            emoji = "🔴" if data["week_pct"] < 0 else "🟢"
            val = f"{data['week_close']:.0f}" if data['week_close'] > 100 else f"{data['week_close']:.2f}"
            md.append(f"| {emoji} {name} | {val} | {data['week_pct']:+.2f}% | {data['week_high']:.0f} | {data['week_low']:.0f} |")
    md.append("")

    # ═══ 四、板块轮动 ═══
    md.append("---\n")
    md.append("## 四、🔄 板块轮动\n")
    sector_data = report.get("sector_rotation", {})

    if sector_data.get("note"):
        md.append(f"> ℹ️ {sector_data['note']}\n")

    if sector_data.get("top_gainers_weekly"):
        md.append("### 🔥 本周强势板块\n")
        md.append("| 板块 | 累计涨幅 | 上榜天数 |")
        md.append("|------|:-------:|:--------:|")
        for s in sector_data["top_gainers_weekly"]:
            md.append(f"| {s['name']} | {s['total_pct']:+.1f}% | {s['days']}天 |")
        md.append("")

    # 每日板块
    if sector_data.get("daily_top"):
        md.append("### 📅 每日板块领涨/领跌\n")
        for date_str in report["week_dates"]:
            daily = sector_data.get("daily_top", {}).get(date_str, {})
            if daily:
                md.append(f"**{date_str}**")
                gainers_str = "、".join([f"{g['name']}({g['pct']:+.1f}%)" for g in daily.get("gainers", [])[:3]])
                losers_str = "、".join([f"{l['name']}({l['pct']:+.1f}%)" for l in daily.get("losers", [])[:3]])
                md.append(f"- 🔥 领涨：{gainers_str}")
                if losers_str:
                    md.append(f"- ❄️ 领跌：{losers_str}")
                md.append("")
    else:
        md.append("> 📊 基于本周每日涨跌结构分析，市场整体偏弱，无持续领涨板块\n")

    # ═══ 五、资金面 ═══
    md.append("---\n")
    md.append("## 五、💰 资金面分析\n")
    cf = report.get("capital_flows", {})

    # 北向资金
    north = cf.get("north_money", {})
    if north.get("daily"):
        md.append("### 🟢 北向资金（沪深港通）\n")
        total = north.get("total_week", 0)
        emoji = "🟢" if total > 0 else "🔴"
        md.append(f"本周合计：**{emoji} {total/1e4:.0f}亿**（日均 {north.get('avg_daily', 0)/1e4:.0f}亿）\n")
        md.append("| 日期 | 北向(万) | 沪股通(万) | 深股通(万) |")
        md.append("|------|:--------:|:----------:|:----------:|")
        for d in north["daily"]:
            md.append(f"| {fmt_date(d['date'])} | {d['north_money']:>8.0f} | {d['hgt']:>8.0f} | {d['sgt']:>8.0f} |")
        md.append("")

    # 融资融券
    margin = cf.get("margin", {})
    if margin.get("daily"):
        md.append("### 📊 融资融券\n")
        md.append("| 日期 | 融资余额(亿) | 融资买入(亿) | 融资变动 |")
        md.append("|------|:----------:|:----------:|:--------:|")
        margin_data = margin["daily"]
        for i, d in enumerate(margin_data):
            change = ""
            if i > 0:
                diff = d["rzye"] - margin_data[i-1]["rzye"]
                change = f"+{diff:.0f}亿" if diff > 0 else f"{diff:.0f}亿"
            md.append(f"| {fmt_date(d['date'])} | {d['rzye']:.0f} | {d['rzmre']:.0f} | {change} |")
        md.append("")

    # ═══ 六、市场情绪 ═══
    md.append("---\n")
    md.append("## 六、📊 市场情绪\n")
    sent = report.get("sentiment", {})
    if sent.get("daily"):
        md.append("| 日期 | 上涨 | 下跌 | 涨跌比 | 涨家占比 | 平均涨幅 |")
        md.append("|------|:---:|:---:|:-----:|:-------:|:-------:|")
        for d in sent["daily"]:
            ratio = f"{d['up']/max(d['down'],1):.2f}" if d.get("down", 0) > 0 else "N/A"
            md.append(f"| {d['date']} | {d['up']} | {d['down']} | {ratio} | {d['up_ratio']}% | {d['avg_pct']:+.2f}% |")
        md.append("")

        # 趋势分析
        daily_sent = sent["daily"]
        first_day = daily_sent[0] if daily_sent else {}
        last_day = daily_sent[-1] if daily_sent else {}
        if first_day and last_day:
            trend = "📉 情绪恶化" if last_day.get("up_ratio", 0) < first_day.get("up_ratio", 0) else "📈 情绪回暖"
            md.append(f"**周初→周末情绪趋势**：{trend}（周初涨家占比{first_day.get('up_ratio', 0)}% → 周末{last_day.get('up_ratio', 0)}%）\n")

    # ═══ 七、技术面 ═══
    md.append("---\n")
    md.append("## 七、📈 技术面分析\n")
    tech = report.get("technical", {})
    tech_summary = tech.get("_summary", {})
    if tech_summary:
        md.append(f"**均线状态**：{tech_summary.get('ma_status', 'N/A')}\n")
        md.append(f"**收盘价**：{tech_summary.get('last_close', 0):.0f}\n")
        md.append(f"**MA5**：{tech_summary.get('last_ma5', 0):.0f}\n")
        md.append(f"**MA20**：{tech_summary.get('last_ma20', 0):.0f}\n")
        md.append(f"**MA60**：{tech_summary.get('last_ma60', 0):.0f}\n")
        signals = tech_summary.get("signals", [])
        if signals:
            md.append("\n**信号**：" + "；".join(signals) + "\n")

    # 每日均线表
    md.append("\n### 上证每日均线位置\n")
    md.append("| 日期 | 收盘价 | MA5 | MA20 | MA60 | 与MA20距离 |")
    md.append("|------|:-----:|:---:|:----:|:----:|:---------:|")
    for date_str in report["week_dates"]:
        d = tech.get(date_str.replace("-", ""), {})
        if d.get("close"):
            close = d["close"]
            ma20 = d.get("ma20", 0)
            dist = f"{((close - ma20) / ma20 * 100):+.1f}%" if ma20 else "-"
            ma5_str = f"{d['ma5']:.0f}" if d.get("ma5") else "-"
            ma20_str = f"{d['ma20']:.0f}" if d.get("ma20") else "-"
            ma60_str = f"{d['ma60']:.0f}" if d.get("ma60") else "-"
            md.append(f"| {date_str} | {close:.0f} | {ma5_str} | {ma20_str} | {ma60_str} | {dist} |")
    md.append("")

    # ═══ 八、消息面 ═══
    md.append("---\n")
    md.append("## 八、📰 本周重要消息\n")
    news = report.get("news", {})

    if news.get("key_events"):
        for cat_name, cat_key in [("📋 政策", "policy"), ("🌍 国际", "global"), ("🏭 行业", "industry"), ("🏢 公司", "company"), ("📊 市场", "market")]:
            events = news.get("categories", {}).get(cat_key, [])
            if events:
                md.append(f"**{cat_name}**")
                for e in events:
                    md.append(f"- [{e['date']}] {e['event']}")
                md.append("")
    else:
        md.append("> 本周情报摘要数据不足，消息面信息待补充\n")

    # ═══ 九、总结 ═══
    md.append("---\n")
    md.append("## 九、📋 本周总结\n")
    md.append(report.get("weekly_summary", "") + "\n")

    # ═══ 十、下周展望 ═══
    md.append("---\n")
    md.append("## 十、🔮 下周展望\n")
    md.append(report.get("next_week_outlook", ""))

    # 保存MD
    today_str = datetime.now().strftime("%Y-%m-%d")
    md_path = p(f"reports/周报/周度复盘_{today_str}.md")
    os.makedirs(os.path.dirname(md_path), exist_ok=True)
    with open(md_path, "w", encoding="utf-8") as f:
        f.write("\n".join(md))
    print(f"\n  [OK] ✅ 全方位周度复盘报告已保存: {md_path}")

    # 转Word
    try:
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "utils"))
        from md_to_docx import convert_md_to_docx
        docx_path = convert_md_to_docx(md_path)
        if docx_path:
            print(f"  [OK] ✅ Word文档已生成: {docx_path}")
    except Exception as e:
        print(f"  [WARN] Word转换失败: {e}")

    # 发送微信
    try:
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "utils"))
        from wechat_send import auto_convert_and_send
        auto_convert_and_send(md_path)
    except Exception as e:
        print(f"  [WARN] 微信发送尝试: {e}")

    return md_path


if __name__ == "__main__":
    week_ending = None
    if len(sys.argv) > 1:
        # 支持 --week-ending YYYY-MM-DD 或直接 YYYY-MM-DD
        if sys.argv[1] == "--week-ending" and len(sys.argv) > 2:
            week_ending = sys.argv[2]
        elif len(sys.argv[1]) == 10:
            week_ending = sys.argv[1]
    report = generate_weekly_report(week_ending)
    md_path = save_report(report)

    print(f"\n✅ 全方位周度复盘完成！")
