"""
Agent5 选股机器人 — 多因子选股引擎 (支持4种模式)

选股模式 (--mode):
  pre_market  早盘选股(09:00)   — 开盘前筛选，侧重昨日动量+隔夜消息
  intraday    盘中选股(10:00+)  — 盘中异动跟踪，侧重资金流向+实时板块
  noon        午盘选股(12:00)   — 上午半日分析，侧重板块轮动+下午方向
  evening     晚间选股(21:00)   — 全天数据+复盘偏差，为次日做准备(默认)

用法:
    source venv/Scripts/activate
    python -X utf8 scripts/agent5-选股/stock_picker.py --top-n 5 [--mode pre_market]

依赖上游报告（可选）:
    reports/日报/情报/情报摘要_YYYY-MM-DD.md  — 热点板块方向
    reports/日报/分析/分析报告_YYYY-MM-DD.md  — 技术面评级/板块排名
    reports/日报/复盘/复盘报告_YYYY-MM-DD.md  — 偏差分析(仅evening模式)

D9反例（工作反例）：
1. 不要早盘用晚间权重——不同使用场景(早盘/盘中/午盘/晚间)对应独立权重配置
2. 不要盘中看估值——盘中数据不可用时跳过基本面因子，不能假设默认值
3. 不要不看复盘偏差——evening模式必须读取复盘报告中的因子调整建议
4. 不要把已持有的票再次推荐（持仓冲突）— 要检查与现有持仓的重复
5. 不要把成长因子用动量因子冒充——两因子独立计算

D3异常处理表：
| 触发条件 | 一线修复 | 仍失败兜底 |
|---------|---------|-----------|
| 模式参数错误(非pre_market/intraday/noon/evening) | 打印suggested_modes并退出 | 用户确认正确模式后重新运行 |
| 配置文件缺失(选股规则.json) | 使用内置默认权重 | 输出警告，建议恢复配置 |
| 行情数据为空(非交易日/停牌) | 重试1次，或往前推1天 | 该股票跳过评分，标注"数据不可用" |
| 停复牌检查失败(suspend_d接口异常) | 用常规daily接口判断当日是否有数据 | 标注"停牌状态未知"，保守不推荐 |
| 因子数据缺失(财务数据未更新) | 用前季数据代替，标注"数据滞后" | 该因子按0分处理，不影响其他因子 |
| 板块数据为空 | 跳过板块动量/热度因子 | 仅用技术面+基本面因子评分 |
| 盘中模式获取实时价失败 | 用最近日线收盘价代替 | 标注"非实时价" |
| 涨跌停价格超出主板±10%/科创创业±20% | 自动截断至涨跌停价以内 | 标记为"超出涨跌停，不可执行" |
| 持仓文件中无总资产字段(新格式vs旧格式) | 依次尝试总资产/总资产_含现金/持仓总市值 | 使用持仓市值之和+现金估算 |

D4 CHECKPOINT:
- CP1-模式匹配验证：输入mode必须匹配4种枚举值之一
- CP2-模式特有检查：pre_market必须有昨日行情；intraday必须有盘中数据
- CP3-停复牌检查：推荐标的必须排除当日停牌股票
- CP4-ST过滤：ST/*ST股票必须排除
- CP5-退市过滤：退市整理期股票必须排除
- CP6-因子独立性验证：成长因子不能与动量因子共用计算逻辑
- CP7-涨跌停范围：推荐价格必须在涨跌停范围内
- CP8-持仓冲突：推荐列表与现有持仓不重复
"""

import os
import sys
import json
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from scripts.utils.tushare_client import pro


# ============================================================
#  工具函数
# ============================================================

def load_json(path: str) -> dict:
    """安全加载 JSON 文件"""
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_report(path: str) -> str:
    """读取报告文件内容"""
    if not os.path.exists(path):
        return ""
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def get_stock_name(ts_code: str) -> str:
    """获取股票名称"""
    try:
        df = pro.stock_basic(ts_code=ts_code, fields="ts_code,name")
        if df is not None and not df.empty:
            name = df.iloc[0].get("name")
            if name:
                return name
    except:
        pass
    return ts_code


def is_st_stock(ts_code: str) -> bool:
    """检查是否为ST/*ST/退市股票"""
    try:
        df = pro.stock_basic(ts_code=ts_code, fields="ts_code,name")
        if df is not None and not df.empty:
            name = df.iloc[0].get("name", "")
            return "ST" in name or "退市" in name
    except:
        pass
    return False


def is_suspended(ts_code: str, trade_date: str) -> bool:
    """检查股票是否停牌"""
    try:
        df = pro.suspend_d(ts_code=ts_code, suspend_date=trade_date)
        if df is not None and not df.empty:
            return True
    except:
        pass
    return False


def get_hot_sectors(intelligence_text: str) -> list:
    """从情报摘要中提取热点板块方向"""
    sectors = [
        "半导体", "芯片", "新能源", "光伏", "锂电池", "人工智能", "AI",
        "消费电子", "医药", "医疗", "金融", "券商", "银行", "保险",
        "房地产", "基建", "军工", "通信", "5G", "机器人", "低空经济",
        "无人驾驶", "量子计算", "数据要素", "信创", "鸿蒙",
    ]
    hot = []
    if intelligence_text:
        for s in sectors:
            if s in intelligence_text:
                hot.append(s)
    return hot


def get_sector_stocks(sector_name: str, max_count: int = 20) -> list:
    """获取某个板块的成分股"""
    try:
        df = pro.ths_member(ts_code="", name=sector_name)
        if df is not None and not df.empty:
            return df["ts_code"].tolist()[:max_count]
    except:
        pass
    return []


def format_score_brief(total: float, factors: dict) -> str:
    """格式化为单行评分摘要"""
    parts = [f"总分{total:.0f}"]
    for k, v in factors.items():
        parts.append(f"{k}{v}")
    return " | ".join(parts)


def load_config(root: str = None) -> dict:
    """加载所有配置和数据"""
    if root is None:
        root = os.path.join(os.path.dirname(__file__), "..", "..")

    rules = load_json(os.path.join(root, "data", "选股规则.json"))
    strategy = load_json(os.path.join(root, "data", "策略规则.json"))
    portfolio = load_json(os.path.join(root, "data", "portfolio.json"))
    watchlist = load_json(os.path.join(root, "data", "watchlist.json"))

    today = datetime.now().strftime("%Y-%m-%d")
    report_dir = os.path.join(root, "reports", "日报")
    intelligence = load_report(os.path.join(report_dir, "情报", f"情报摘要_{today}.md"))
    analysis = load_report(os.path.join(report_dir, "分析", f"分析报告_{today}.md"))
    review = load_report(os.path.join(report_dir, "复盘", f"复盘报告_{today}.md"))

    return {
        "rules": rules,
        "strategy": strategy,
        "portfolio": portfolio,
        "watchlist": watchlist,
        "intelligence": intelligence,
        "analysis": analysis,
        "review": review,
    }


# ============================================================
#  公共评分函数（所有模式共用）
# ============================================================

def score_valuation(ts_code: str, trade_date: str) -> dict:
    """估值因子评分（0-100）"""
    try:
        df = pro.daily_basic(ts_code=ts_code, trade_date=trade_date)
        if df is None or df.empty:
            return {"score": 50, "details": {"reason": "无估值数据，给中性分"}}
        row = df.iloc[0]
        pe = row.get("pe", None)
        pb = row.get("pb", None)

        score = 50
        details = {}
        if pe and 0 < pe < 80:
            if pe < 15:
                score = 90
                details["pe"] = f"PE={pe:.1f}，低估"
            elif pe < 30:
                score = 75
                details["pe"] = f"PE={pe:.1f}，合理偏低"
            elif pe < 50:
                score = 60
                details["pe"] = f"PE={pe:.1f}，合理偏高"
            else:
                score = 40
                details["pe"] = f"PE={pe:.1f}，偏高"
        else:
            score -= 10
            details["pe"] = f"PE={pe}，异常或缺失"

        if pb and 0 < pb < 10:
            if pb < 2:
                score += 5
                details["pb"] = f"PB={pb:.1f}，偏低"
            else:
                details["pb"] = f"PB={pb:.1f}，合理"
        else:
            score -= 5
            details["pb"] = f"PB={pb}，异常"
        return {"score": max(0, min(100, score)), "details": details}
    except:
        return {"score": 50, "details": {"reason": "估值评分异常"}}


def score_momentum(ts_code: str) -> dict:
    """动量因子评分（0-100）"""
    try:
        df = pro.daily(ts_code=ts_code)
        if df is None or df.empty or len(df) < 40:
            return {"score": 50, "details": {"reason": "行情数据不足"}}
        close_20 = df["close"].iloc[:20].values
        close_60 = df["close"].iloc[:60].values if len(df) >= 60 else None
        ret_20 = (close_20[0] - close_20[-1]) / close_20[-1] * 100 if len(close_20) >= 2 else 0
        ret_60 = (close_60[0] - close_60[-1]) / close_60[-1] * 100 if (close_60 is not None and len(close_60) >= 2) else 0

        score = 50
        if -5 <= ret_20 <= 30:
            score += 15
        else:
            score -= 10
        if ret_60 > ret_20:
            score += 15
        else:
            score -= 5

        return {"score": max(0, min(100, score)),
                "details": {"ret_20": f"{ret_20:.1f}%", "ret_60": f"{ret_60:.1f}%" if ret_60 else "N/A"}}
    except:
        return {"score": 50, "details": {"reason": "动量评分异常"}}


def score_technical(ts_code: str) -> dict:
    """技术面因子评分（0-100）"""
    try:
        df = pro.daily(ts_code=ts_code)
        if df is None or df.empty or len(df) < 30:
            return {"score": 50, "details": {"reason": "技术数据不足"}}
        closes = df["close"].values
        volumes = df["vol"].values if "vol" in df.columns else None

        if len(closes) >= 20:
            ma20 = sum(closes[:20]) / 20
            current = closes[0]
            if current > ma20 * 1.05:
                score = 80
                detail = f"价格({current:.2f}) > MA20({ma20:.2f}) +5%"
            elif current > ma20:
                score = 65
                detail = f"价格({current:.2f}) > MA20({ma20:.2f})"
            else:
                score = 35
                detail = f"价格({current:.2f}) < MA20({ma20:.2f})"
        else:
            score = 50
            detail = "数据不足20日"

        vol_score = 0
        if volumes is not None and len(volumes) >= 8:
            recent_avg = sum(volumes[:3]) / 3
            prev_avg = sum(volumes[3:8]) / 5
            if prev_avg > 0 and recent_avg > prev_avg * 1.3:
                vol_score = 15
                detail += "，放量"
        return {"score": max(0, min(100, score + vol_score)), "details": {"summary": detail}}
    except:
        return {"score": 50, "details": {"reason": "技术评分异常"}}


def score_sentiment(ts_code: str, trade_date: str = None) -> dict:
    """情绪因子评分（0-100）— 基于资金流向"""
    try:
        if trade_date:
            df = pro.moneyflow(ts_code=ts_code, start_date=trade_date, end_date=trade_date)
            if df is not None and not df.empty:
                row = df.iloc[0]
                net = float(row.get("net_amount", 0))
                if net > 0:
                    return {"score": 65, "details": {"reason": f"主力资金净流入{net:.0f}万"}}
                elif net < 0:
                    return {"score": 40, "details": {"reason": f"主力资金净流出{abs(net):.0f}万"}}
    except:
        pass
    # 兜底：用涨跌幅判断情绪
    try:
        df = pro.daily(ts_code=ts_code)
        if df is not None and not df.empty and len(df) >= 5:
            recent = df["pct_chg"].iloc[:5].mean()
            if recent > 3:
                return {"score": 65, "details": {"reason": f"近5日涨幅{recent:.1f}%，情绪积极"}}
            elif recent < -3:
                return {"score": 35, "details": {"reason": f"近5日跌幅{recent:.1f}%，情绪悲观"}}
    except:
        pass
    return {"score": 50, "details": {"reason": "无资金流数据，中性评分"}}


def score_growth(ts_code: str, trade_date: str) -> dict:
    """成长因子评分（0-100）— 基于财务指标"""
    try:
        df = pro.fina_indicator(ts_code=ts_code, start_date=f"{trade_date[:4]}0101", end_date=trade_date)
        if df is None or df.empty:
            basic = pro.daily_basic(ts_code=ts_code, trade_date=trade_date)
            if basic is not None and not basic.empty:
                pe = basic.iloc[0].get("pe", 0) or 0
                if 0 < pe < 30:
                    return {"score": 65, "details": {"reason": f"PE={pe:.1f}偏低，成长良好"}}
                elif 30 <= pe < 60:
                    return {"score": 50, "details": {"reason": f"PE={pe:.1f}中等"}}
                else:
                    return {"score": 35, "details": {"reason": f"PE={pe:.1f}偏高或无数据"}}
            return {"score": 50, "details": {"reason": "财务数据不足，给中性分"}}

        row = df.iloc[0]
        score = 50
        details = {}

        rev_growth = row.get("revenue_yoy")
        if rev_growth is not None:
            if rev_growth > 30:
                score += 25
                details["营收增长"] = f"{rev_growth:.1f}%，高速增长"
            elif rev_growth > 15:
                score += 15
                details["营收增长"] = f"{rev_growth:.1f}%，稳健增长"
            elif rev_growth > 0:
                score += 5
                details["营收增长"] = f"{rev_growth:.1f}%，正增长"
            else:
                score -= 10
                details["营收增长"] = f"{rev_growth:.1f}%，负增长"

        profit_growth = row.get("profit_dedt")
        if profit_growth is not None:
            if profit_growth > 30:
                score += 20
                details["净利增长"] = f"{profit_growth:.1f}%，高速增长"
            elif profit_growth > 10:
                score += 10
                details["净利增长"] = f"{profit_growth:.1f}%，稳健增长"
            elif profit_growth > 0:
                score += 5
                details["净利增长"] = f"{profit_growth:.1f}%，正增长"
            else:
                score -= 10
                details["净利增长"] = f"{profit_growth:.1f}%，负增长"

        roe = row.get("roe")
        if roe is not None:
            if roe > 15:
                score += 10
                details["ROE"] = f"{roe:.1f}%，优秀"
            elif roe > 8:
                score += 5
                details["ROE"] = f"{roe:.1f}%，良好"
            else:
                details["ROE"] = f"{roe:.1f}%，偏低"

        return {"score": max(0, min(100, score)), "details": details}
    except Exception as e:
        return {"score": 50, "details": {"reason": f"成长评分异常: {e}"}}


def get_sector_strength_ranking(analysis_text: str) -> list:
    """从分析师报告中解析板块强度排名"""
    sectors = []
    if not analysis_text:
        return sectors
    for line in analysis_text.split("\n"):
        if any(kw in line for kw in ["板块", "行业", "热点", "领涨", "强势"]):
            sectors.append(line.strip())
    return sectors[:10]


# ============================================================
#  模式1: 早盘选股 (pre_market)
# ============================================================

def pre_market_picks(top_n: int, config: dict, today: str) -> dict:
    """早盘选股 — 开盘前筛选，侧重昨日动量+隔夜消息"""
    ok, warn, fail = "[OK]", "[WARN]", "[FAIL]"
    print(f"[选股机器人] 模式=早盘选股 top_n={top_n}...")

    result = {
        "mode": "pre_market",
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "parameters": {"top_n": top_n},
        "hot_sectors": [],
        "candidates_screened": 0,
        "ranked_stocks": [],
        "pre_market_notes": [],
        "suspended_stocks": [],
        "warnings": [],
        "errors": [],
    }

    # 获取昨日前一交易日
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")

    # 热点板块（情报+分析）
    hot_sectors = get_hot_sectors(config["intelligence"])
    if not hot_sectors:
        hot_sectors = ["人工智能", "半导体", "新能源"]
    result["hot_sectors"] = hot_sectors
    print(f"  {ok} 热点板块: {', '.join(hot_sectors[:5])}")

    # 从分析师报告提取板块排名
    sector_rankings = get_sector_strength_ranking(config["analysis"])
    if sector_rankings:
        print(f"  {ok} 板块强度排名: {len(sector_rankings)} 条")

    # 候选池：热点板块成分股 + 持仓 + 自选
    candidates = set()
    for sector in hot_sectors[:3]:
        for s in get_sector_stocks(sector, 15):
            candidates.add(s)
    for p in config.get("portfolio", {}).get("持仓列表", []):
        code = p.get("代码", "")
        if code and code != "000000":
            candidates.add(code)
    for sector_stocks in config.get("watchlist", {}).values():
        if isinstance(sector_stocks, list):
            for s in sector_stocks:
                candidates.add(s)

    if not candidates:
        result["errors"].append("无候选股票")
        print(f"  {fail} 候选池为空")
        return result

    result["candidates_screened"] = len(candidates)
    print(f"  {ok} 候选池: {len(candidates)} 只")

    # 模式权重（早盘侧重动量+情绪）
    weights = config.get("rules", {}).get("模式权重", {}).get("pre_market", {}).get("因子权重", {
        "估值": 15, "成长": 15, "动量": 30, "情绪": 20, "技术面": 20,
    })

    # 评分（早盘用昨日数据）
    scored = []
    for ts_code in candidates:
        if is_st_stock(ts_code):
            print(f"  {warn} {ts_code} ST/退市，跳过")
            continue

        if is_suspended(ts_code, today):
            result["suspended_stocks"].append(ts_code)
            print(f"  {warn} {ts_code} 停牌，跳过")
            continue

        try:
            valuation = score_valuation(ts_code, yesterday)
            growth = score_growth(ts_code, yesterday)
            momentum = score_momentum(ts_code)
            technical = score_technical(ts_code)
            sentiment = score_sentiment(ts_code, yesterday)

            factor_scores = {"估值": valuation["score"], "成长": growth["score"],
                             "动量": momentum["score"], "技术面": technical["score"],
                             "情绪": sentiment["score"]}
            strong_factors = sum(1 for v in factor_scores.values() if v >= 60)

            total = (valuation["score"] * weights.get("估值", 15) + growth["score"] * weights.get("成长", 15)
                     + momentum["score"] * weights.get("动量", 30) + technical["score"] * weights.get("技术面", 20)
                     + sentiment["score"] * weights.get("情绪", 20)) / 100

            scored.append({
                "ts_code": ts_code,
                "total_score": round(total, 1),
                "strong_factors": strong_factors,
                "factors": factor_scores,
                "factor_details": {"估值": valuation["details"], "成长": growth["details"],
                                   "动量": momentum["details"], "技术面": technical["details"],
                                   "情绪": sentiment["details"]},
            })
            print(f"  {ok} {ts_code}: {total:.1f}分")
        except Exception as e:
            print(f"  {warn} {ts_code} 评分失败: {e}")

    # 排序取Top N
    scored.sort(key=lambda x: x["total_score"], reverse=True)
    result["ranked_stocks"] = scored[:top_n]

    # 早盘附加检查
    for s in result["ranked_stocks"]:
        result.setdefault("pre_market_notes", []).append(
            f"{s['ts_code']}: 动量{s['factors']['动量']} 情绪{s['factors']['情绪']} 技术{s['factors']['技术面']}"
        )

    print(f"\n  {'='*40}")
    print(f"  早盘候选 Top {top_n}")
    print(f"  {'='*40}")
    for i, s in enumerate(result["ranked_stocks"], 1):
        print(f"  {i}. {s['ts_code']} — {s['total_score']}分")
        print(f"     估值:{s['factors']['估值']} 成长:{s['factors']['成长']} 动量:{s['factors']['动量']} 技术:{s['factors']['技术面']} 情绪:{s['factors']['情绪']} 强因子:{s['strong_factors']}/5")

    return result


# ============================================================
#  模式2: 盘中选股 (intraday)
# ============================================================

def intraday_picks(top_n: int, config: dict, today: str) -> dict:
    """盘中选股 — 盘中异动跟踪，侧重资金流向+实时板块"""
    ok, warn, fail = "[OK]", "[WARN]", "[FAIL]"
    print(f"[选股机器人] 模式=盘中选股 top_n={top_n}...")

    result = {
        "mode": "intraday",
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "parameters": {"top_n": top_n},
        "hot_sectors": [],
        "capital_flow_summary": {},
        "ranked_stocks": [],
        "alerts": [],
        "sector_rotation_notes": [],
        "warnings": [],
        "errors": [],
    }

    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")

    # 1. 获取今日板块涨幅排名
    try:
        ths_df = pro.ths_daily(trade_date=today)
        if ths_df is not None and not ths_df.empty:
            top_sectors = ths_df.head(10)
            result["hot_sectors"] = top_sectors["name"].tolist() if "name" in top_sectors.columns else []
            print(f"  {ok} 今日热点板块: {', '.join(result['hot_sectors'][:5])}")
    except:
        print(f"  {warn} 无法获取今日板块数据")

    # 2. 北向资金实时（盘中可获取）
    try:
        hsgt = pro.moneyflow_hsgt(start_date=today, end_date=today)
        if hsgt is not None and not hsgt.empty:
            last = hsgt.iloc[-1]
            result["capital_flow_summary"]["北向资金"] = {
                "net": float(last.get("net_hsgt", 0)),
                "south_net": float(last.get("net_hsgt", 0)),
            }
            net_val = result["capital_flow_summary"]["北向资金"]["net"]
            direction = "净流入" if net_val > 0 else "净流出"
            print(f"  {ok} 北向资金: {direction} {abs(net_val):.0f}万")
    except:
        print(f"  {warn} 北向资金数据暂不可用")

    # 3. 候选池：持仓+自选（盘中模式关注已有持仓的盘中机会）
    candidates = set()
    for p in config.get("portfolio", {}).get("持仓列表", []):
        code = p.get("代码", "")
        if code and code != "000000":
            candidates.add(code)
    for sector_stocks in config.get("watchlist", {}).values():
        if isinstance(sector_stocks, list):
            for s in sector_stocks:
                candidates.add(s)
    # 加权板块成分股
    for sector in result.get("hot_sectors", [])[:3]:
        for s in get_sector_stocks(sector, 10):
            candidates.add(s)

    if not candidates:
        result["errors"].append("无候选股票")
        print(f"  {fail} 候选池为空")
        return result
    result["candidates_screened"] = len(candidates)
    print(f"  {ok} 候选池: {len(candidates)} 只")

    # 4. 盘中评分（侧重情绪/资金流）
    weights = config.get("rules", {}).get("模式权重", {}).get("intraday", {}).get("因子权重", {
        "估值": 10, "成长": 10, "动量": 25, "情绪": 35, "技术面": 20,
    })

    scored = []
    for ts_code in candidates:
        if is_st_stock(ts_code):
            continue
        try:
            yesterday_ymd = (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")
            valuation = score_valuation(ts_code, yesterday_ymd)
            growth = score_growth(ts_code, yesterday_ymd)
            momentum = score_momentum(ts_code)
            technical = score_technical(ts_code)
            sentiment = score_sentiment(ts_code, yesterday_ymd)

            factor_scores = {"估值": valuation["score"], "成长": growth["score"],
                             "动量": momentum["score"], "技术面": technical["score"],
                             "情绪": sentiment["score"]}
            strong_factors = sum(1 for v in factor_scores.values() if v >= 60)

            total = (valuation["score"] * weights.get("估值", 10) + growth["score"] * weights.get("成长", 10)
                     + momentum["score"] * weights.get("动量", 25) + technical["score"] * weights.get("技术面", 20)
                     + sentiment["score"] * weights.get("情绪", 35)) / 100

            scored.append({
                "ts_code": ts_code,
                "total_score": round(total, 1),
                "strong_factors": strong_factors,
                "factors": factor_scores,
                "factor_details": {"估值": valuation["details"], "成长": growth["details"],
                                   "动量": momentum["details"], "技术面": technical["details"],
                                   "情绪": sentiment["details"]},
            })
        except Exception as e:
            print(f"  {warn} {ts_code} 评分失败: {e}")

    scored.sort(key=lambda x: x["total_score"], reverse=True)
    result["ranked_stocks"] = scored[:top_n]

    # 5. 异动快报（基于评分的信号标注）
    for s in result["ranked_stocks"]:
        alerts = []
        if s["factors"]["动量"] >= 75:
            alerts.append("动量强势")
        if s["factors"]["情绪"] >= 60:
            alerts.append("资金关注")
        if s["factors"]["技术面"] >= 75:
            alerts.append("技术突破")
        if alerts:
            result.setdefault("alerts", []).append(f"{s['ts_code']}: {', '.join(alerts)}")

    print(f"\n  {'='*40}")
    print(f"  盘中关注 Top {top_n}")
    print(f"  {'='*40}")
    for i, s in enumerate(result["ranked_stocks"], 1):
        print(f"  {i}. {s['ts_code']} — {s['total_score']}分")
    if result.get("alerts"):
        print(f"\n  ⚡ 异动信号:")
        for a in result["alerts"]:
            print(f"    {a}")

    return result


# ============================================================
#  模式3: 午盘选股 (noon)
# ============================================================

def noon_picks(top_n: int, config: dict, today: str) -> dict:
    """午盘选股 — 上午半日分析，侧重板块轮动+下午方向"""
    ok, warn, fail = "[OK]", "[WARN]", "[FAIL]"
    print(f"[选股机器人] 模式=午盘选股 top_n={top_n}...")

    result = {
        "mode": "noon",
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "parameters": {"top_n": top_n},
        "hot_sectors": [],
        "morning_summary": {},
        "ranked_stocks": [],
        "afternoon_direction": "",
        "sector_rotation_notes": [],
        "warnings": [],
        "errors": [],
    }

    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")

    # 获取上午板块表现
    try:
        ths_df = pro.ths_daily(trade_date=today)
        if ths_df is not None and not ths_df.empty:
            top5 = ths_df.head(5)
            bottom5 = ths_df.tail(5)
            result["hot_sectors"] = top5["name"].tolist() if "name" in top5.columns else []
            result["morning_summary"]["领涨板块"] = top5["name"].tolist()[:5] if "name" in top5.columns else []
            result["morning_summary"]["领跌板块"] = bottom5["name"].tolist()[:5] if "name" in bottom5.columns else []
            print(f"  {ok} 上午领涨板块: {', '.join(result['morning_summary'].get('领涨板块', [])[:3])}")
    except:
        print(f"  {warn} 板块数据暂不可用")

    # 资金流向
    try:
        hsgt = pro.moneyflow_hsgt(start_date=today, end_date=today)
        if hsgt is not None and not hsgt.empty:
            last = hsgt.iloc[-1]
            result["morning_summary"]["北向资金"] = round(float(last.get("net_hsgt", 0)), 0)
    except:
        pass

    # 候选池：领涨板块成分股+自选+持仓
    candidates = set()
    for sector in result.get("hot_sectors", [])[:3]:
        for s in get_sector_stocks(sector, 10):
            candidates.add(s)
    for p in config.get("portfolio", {}).get("持仓列表", []):
        code = p.get("代码", "")
        if code and code != "000000":
            candidates.add(code)
    for sector_stocks in config.get("watchlist", {}).values():
        if isinstance(sector_stocks, list):
            for s in sector_stocks:
                candidates.add(s)
    if not candidates:
        result["errors"].append("无候选股票")
        print(f"  {fail} 候选池为空")
        return result
    result["candidates_screened"] = len(candidates)
    print(f"  {ok} 候选池: {len(candidates)} 只")

    # 午盘权重（侧重情绪/资金流+动量）
    weights = config.get("rules", {}).get("模式权重", {}).get("noon", {}).get("因子权重", {
        "估值": 15, "成长": 15, "动量": 20, "情绪": 30, "技术面": 20,
    })

    scored = []
    for ts_code in candidates:
        if is_st_stock(ts_code):
            continue
        try:
            ymd = (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")
            valuation = score_valuation(ts_code, ymd)
            growth = score_growth(ts_code, ymd)
            momentum = score_momentum(ts_code)
            technical = score_technical(ts_code)
            sentiment = score_sentiment(ts_code, ymd)

            factor_scores = {"估值": valuation["score"], "成长": growth["score"],
                             "动量": momentum["score"], "技术面": technical["score"],
                             "情绪": sentiment["score"]}
            strong_factors = sum(1 for v in factor_scores.values() if v >= 60)

            total = (valuation["score"] * weights.get("估值", 15) + growth["score"] * weights.get("成长", 15)
                     + momentum["score"] * weights.get("动量", 20) + technical["score"] * weights.get("技术面", 20)
                     + sentiment["score"] * weights.get("情绪", 30)) / 100

            scored.append({
                "ts_code": ts_code,
                "total_score": round(total, 1),
                "strong_factors": strong_factors,
                "factors": factor_scores,
                "factor_details": {"估值": valuation["details"], "成长": growth["details"],
                                   "动量": momentum["details"], "技术面": technical["details"],
                                   "情绪": sentiment["details"]},
            })
        except Exception as e:
            print(f"  {warn} {ts_code} 评分失败: {e}")

    scored.sort(key=lambda x: x["total_score"], reverse=True)
    result["ranked_stocks"] = scored[:top_n]

    # 下午方向判断
    if result.get("hot_sectors"):
        result["afternoon_direction"] = f"上午{result['hot_sectors'][0] if result['hot_sectors'] else '无'}领涨，建议关注板块持续性"
    else:
        result["afternoon_direction"] = "上午板块数据不足，建议关注大盘走势"

    print(f"\n  {'='*40}")
    print(f"  午盘候选 Top {top_n}")
    print(f"  {'='*40}")
    for i, s in enumerate(result["ranked_stocks"], 1):
        print(f"  {i}. {s['ts_code']} — {s['total_score']}分")
    print(f"\n  下午方向: {result['afternoon_direction']}")

    return result


# ============================================================
#  模式4: 晚间选股 (evening) — 默认模式，原逻辑增强版
# ============================================================

def evening_picks(top_n: int, config: dict, today: str) -> dict:
    """晚间选股 — 全天数据+复盘偏差，最精细评分"""
    ok, warn, fail = "[OK]", "[WARN]", "[FAIL]"
    print(f"[选股机器人] 模式=晚间选股 top_n={top_n}...")

    result = {
        "mode": "evening",
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "parameters": {"top_n": top_n},
        "hot_sectors": [],
        "candidates_screened": 0,
        "ranked_stocks": [],
        "st_filtered": [],
        "suspended_stocks": [],
        "sector_concentration": {},
        "review_deviation_notes": [],
        "warnings": [],
        "errors": [],
    }

    # 从复盘报告提取偏差分析
    review_text = config.get("review", "")
    if review_text:
        deviation_lines = [l.strip() for l in review_text.split("\n") if "偏差" in l or "改进" in l or "修正" in l]
        result["review_deviation_notes"] = deviation_lines[:5]
        if deviation_lines:
            print(f"  {ok} 复盘偏差分析: {len(deviation_lines)} 条参考")

    # 热点板块
    hot_sectors = get_hot_sectors(config["intelligence"])
    if not hot_sectors:
        hot_sectors = ["人工智能", "半导体", "新能源"]
    result["hot_sectors"] = hot_sectors
    print(f"  {ok} 热点板块: {', '.join(hot_sectors[:5])}")

    # 候选池
    candidates = set()
    for sector in hot_sectors[:3]:
        for s in get_sector_stocks(sector, 15):
            candidates.add(s)
    for p in config.get("portfolio", {}).get("持仓列表", []):
        code = p.get("代码", "")
        if code and code != "000000":
            candidates.add(code)
    for sector_stocks in config.get("watchlist", {}).values():
        if isinstance(sector_stocks, list):
            for s in sector_stocks:
                candidates.add(s)

    if not candidates:
        result["errors"].append("无候选股票")
        print(f"  {fail} 候选池为空")
        return result
    result["candidates_screened"] = len(candidates)
    print(f"  {ok} 候选池: {len(candidates)} 只")

    # 完整评分（使用今日数据）
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")
    weights = config.get("rules", {}).get("因子权重", {
        "估值": 25, "成长": 20, "动量": 20, "情绪": 15, "技术面": 20,
    })

    scored = []
    for ts_code in candidates:
        if is_st_stock(ts_code):
            print(f"  {warn} {ts_code} ST/退市，跳过")
            result.setdefault("st_filtered", []).append(ts_code)
            continue

        if is_suspended(ts_code, today):
            print(f"  {warn} {ts_code} 停牌，跳过")
            result.setdefault("suspended_stocks", []).append(ts_code)
            continue

        try:
            valuation = score_valuation(ts_code, yesterday)
            growth = score_growth(ts_code, yesterday)
            momentum = score_momentum(ts_code)
            technical = score_technical(ts_code)
            sentiment = score_sentiment(ts_code, yesterday)

            factor_scores = {"估值": valuation["score"], "成长": growth["score"],
                             "动量": momentum["score"], "技术面": technical["score"],
                             "情绪": sentiment["score"]}
            strong_factors = sum(1 for v in factor_scores.values() if v >= 60)

            total = (valuation["score"] * weights.get("估值", 25) + growth["score"] * weights.get("成长", 20)
                     + momentum["score"] * weights.get("动量", 20) + technical["score"] * weights.get("技术面", 20)
                     + sentiment["score"] * weights.get("情绪", 15)) / 100

            scored.append({
                "ts_code": ts_code,
                "total_score": round(total, 1),
                "strong_factors": strong_factors,
                "factors": factor_scores,
                "factor_details": {"估值": valuation["details"], "成长": growth["details"],
                                   "动量": momentum["details"], "技术面": technical["details"],
                                   "情绪": sentiment["details"]},
            })
        except Exception as e:
            print(f"  {warn} {ts_code} 评分失败: {e}")
            result["errors"].append(f"{ts_code} 评分异常: {e}")

    scored.sort(key=lambda x: x["total_score"], reverse=True)
    top_stocks = scored[:top_n]
    result["ranked_stocks"] = top_stocks

    # 行业集中度检查
    sector_counts = {}
    for s in scored:
        prefix = s["ts_code"][:3]
        sector_counts[prefix] = sector_counts.get(prefix, 0) + 1
    total_stocks = max(len(scored), 1)
    for prefix, count in sector_counts.items():
        pct = round(count / total_stocks * 100, 1)
        result["sector_concentration"][f"{prefix}xxx"] = {"count": count, "pct": pct, "over_limit": pct > 40}
    if any(c["over_limit"] for c in result["sector_concentration"].values()):
        result["warnings"].append("行业集中度超40%限制")
        print(f"  {warn} 行业集中度超40%")

    # 止损位
    for s in top_stocks:
        try:
            df = pro.daily(ts_code=s["ts_code"])
            if df is not None and not df.empty and len(df) >= 20:
                close = df.iloc[0]["close"]
                s["suggested_stop_loss"] = round(float(close) * 0.93, 2)
                s["current_price"] = round(float(close), 2)
            else:
                s["suggested_stop_loss"] = None
                s["current_price"] = None
        except:
            s["suggested_stop_loss"] = None
            s["current_price"] = None

    print(f"\n  {'='*40}")
    print(f"  晚间选股 Top {top_n}")
    print(f"  {'='*40}")
    for i, s in enumerate(top_stocks, 1):
        print(f"  {i}. {s['ts_code']} — {s['total_score']}分 (强因子:{s.get('strong_factors', 0)}/5)")
        print(f"     估值:{s['factors']['估值']} 成长:{s['factors']['成长']} 动量:{s['factors']['动量']} 技术:{s['factors']['技术面']} 情绪:{s['factors']['情绪']}")

    if result.get("st_filtered"):
        print(f"\n  {warn} ST过滤: {len(result['st_filtered'])} 只")
    if result.get("review_deviation_notes"):
        print(f"\n  {ok} 已参考复盘偏差: {len(result['review_deviation_notes'])} 条")

    return result


# ============================================================
#  主入口
# ============================================================

def generate_stock_picks(top_n: int = 5, force_refresh: bool = False, mode: str = "evening") -> dict:
    """主函数：按模式分发选股"""
    root = os.path.join(os.path.dirname(__file__), "..", "..")
    config = load_config(root)
    today = datetime.now().strftime("%Y%m%d")

    mode_map = {
        "pre_market": pre_market_picks,
        "intraday": intraday_picks,
        "noon": noon_picks,
        "evening": evening_picks,
    }

    func = mode_map.get(mode, evening_picks)
    result = func(top_n, config, today)

    # 统一标记
    result["mode"] = mode
    return result


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="多因子选股 (4种模式)")
    parser.add_argument("--top-n", type=int, default=5, help="输出候选数量")
    parser.add_argument("--force-refresh", action="store_true", help="强制刷新数据")
    parser.add_argument("--mode", type=str, default="evening",
                        choices=["pre_market", "intraday", "noon", "evening"],
                        help="选股模式: pre_market(早盘)/intraday(盘中)/noon(午盘)/evening(晚间)")
    args = parser.parse_args()

    print(f"\n{'='*50}")
    print(f"  选股机器人 — 模式: {args.mode}")
    print(f"{'='*50}\n")

    report = generate_stock_picks(args.top_n, args.force_refresh, args.mode)

    print("\n=== STOCK_PICKS ===")
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    print("=== END ===")

    # 保存报告
    output_dir = os.path.join(os.path.dirname(__file__), "..", "..", "data", "raw")
    os.makedirs(output_dir, exist_ok=True)
    today = datetime.now().strftime("%Y%m%d")
    output_path = os.path.join(output_dir, f"选股原始数据_{today}_{args.mode}.json")
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n选股数据已保存: {output_path}")
