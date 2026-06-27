"""
Agent5 选股机器人 — 多因子选股引擎

功能：
1. 读取选股规则、策略规则、持仓等配置
2. 获取全市场日线行情、财务指标、资金流向
3. 多因子评分筛选候选股票
4. 输出选股建议报告

用法：
    source venv/Scripts/activate
    python -X utf8 scripts/agent5-选股/stock_picker.py [--top-n 5] [--force-refresh]

依赖上游报告（可选）：
    reports/日报/情报/情报摘要_YYYY-MM-DD.md  — 热点板块方向
    reports/日报/分析/分析报告_YYYY-MM-DD.md  — 技术面评级
"""

import os
import sys
import json
import glob
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from scripts.utils.tushare_client import pro


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


def load_config():
    """加载所有配置和数据"""
    root = os.path.join(os.path.dirname(__file__), "..", "..")

    rules = load_json(os.path.join(root, "data", "选股规则.json"))
    strategy = load_json(os.path.join(root, "data", "策略规则.json"))
    portfolio = load_json(os.path.join(root, "data", "portfolio.json"))
    watchlist = load_json(os.path.join(root, "data", "watchlist.json"))

    # 读取上游报告（如果存在）
    today = datetime.now().strftime("%Y-%m-%d")
    report_dir = os.path.join(root, "reports", "日报")
    intelligence = load_report(os.path.join(report_dir, "情报", f"情报摘要_{today}.md"))
    analysis = load_report(os.path.join(report_dir, "分析", f"分析报告_{today}.md"))

    return {
        "rules": rules,
        "strategy": strategy,
        "portfolio": portfolio,
        "watchlist": watchlist,
        "intelligence": intelligence,
        "analysis": analysis,
    }


def get_hot_sectors(intelligence_text: str) -> list:
    """从情报摘要中提取热点板块方向（简单关键词提取）"""
    hot_keywords = []
    # 常见板块关键词
    sectors = [
        "半导体", "芯片", "新能源", "光伏", "锂电池", "人工智能", "AI",
        "消费电子", "医药", "医疗", "金融", "券商", "银行", "保险",
        "房地产", "基建", "军工", "通信", "5G", "机器人", "低空经济",
        "无人驾驶", "量子计算", "数据要素", "信创", "鸿蒙",
    ]
    if intelligence_text:
        for s in sectors:
            if s in intelligence_text:
                hot_keywords.append(s)
    return hot_keywords


def get_sector_stocks(sector_name: str) -> list:
    """获取某个板块的成分股（简化版：从概念板块获取）"""
    try:
        # 用同花顺概念板块查
        df = pro.ths_member(ts_code="", name=sector_name)
        if df is not None and not df.empty:
            return df["ts_code"].tolist()[:20]  # 最多取前20
    except:
        pass
    return []


def fetch_candidates(settings: dict, hot_sectors: list) -> list:
    """
    获取候选股票池。
    简化实现：从热点板块成分股 + 持仓 + 自选 中初步筛选。
    """
    candidates = set()

    # 1. 热点板块成分股
    for sector in hot_sectors[:3]:
        stocks = get_sector_stocks(sector)
        for s in stocks:
            candidates.add(s)

    # 2. 持仓股票（检查是否要加仓/减仓）
    portfolio = settings.get("portfolio", {}).get("持仓列表", [])
    for p in portfolio:
        code = p.get("代码", "")
        if code and code != "000000":
            candidates.add(code)

    # 3. 自选股
    watchlist = settings.get("watchlist", {})
    for sector_stocks in watchlist.values():
        if isinstance(sector_stocks, list):
            for s in sector_stocks:
                candidates.add(s)

    return list(candidates)


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

        # 短期涨幅在合理范围
        if -5 <= ret_20 <= 30:
            score += 15
        else:
            score -= 10

        # 中期趋势强于短期（趋势延续）
        if ret_60 > ret_20:
            score += 15
        else:
            score -= 5

        return {
            "score": max(0, min(100, score)),
            "details": {
                "ret_20": f"{ret_20:.1f}%",
                "ret_60": f"{ret_60:.1f}%" if ret_60 else "N/A",
            },
        }
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
        # 简化评分：最近价格在20日均线之上得分更高
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

        # 成交量放大检查
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


def score_sentiment(ts_code: str) -> dict:
    """情绪因子评分（暂简化为中性分）"""
    # 简化版：在完整实现中应接入北向资金、主力资金数据
    return {"score": 50, "details": {"reason": "情绪因子简化评分（需接入资金流数据）"}}


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


def generate_stock_picks(top_n: int = 5, force_refresh: bool = False) -> dict:
    """主函数：生成选股建议"""
    print(f"[选股机器人] 开始多因子选股 (top_n={top_n})...")
    ok = "[OK]"
    warn = "[WARN]"
    fail = "[FAIL]"

    result = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "parameters": {"top_n": top_n, "force_refresh": force_refresh},
        "hot_sectors": [],
        "candidates_screened": 0,
        "ranked_stocks": [],
        "errors": [],
    }

    # 1. 加载配置
    config = load_config()
    today = datetime.now().strftime("%Y%m%d")

    # 2. 提取热点板块
    hot_sectors = get_hot_sectors(config["intelligence"])
    if not hot_sectors:
        hot_sectors = ["人工智能", "半导体", "新能源"]  # 默认热点
    result["hot_sectors"] = hot_sectors
    print(f"  {ok} 热点板块: {', '.join(hot_sectors[:5])}")

    # 3. 获取候选池
    candidates = fetch_candidates(config, hot_sectors)
    if not candidates:
        result["errors"].append("无候选股票")
        print(f"  {fail} 候选池为空")
        return result

    result["candidates_screened"] = len(candidates)
    print(f"  {ok} 候选池: {len(candidates)} 只")

    # 4. 多因子评分
    scored_stocks = []
    weights = config.get("rules", {}).get("因子权重", {
        "估值": 25, "成长": 20, "动量": 20, "情绪": 15, "技术面": 20,
    })

    for ts_code in candidates:
        try:
            valuation = score_valuation(ts_code, today)
            momentum = score_momentum(ts_code)
            technical = score_technical(ts_code)
            sentiment = score_sentiment(ts_code)

            total = (
                valuation["score"] * weights.get("估值", 25)
                + momentum["score"] * weights.get("动量", 20)
                + technical["score"] * weights.get("技术面", 20)
                + sentiment["score"] * weights.get("情绪", 15)
                # 成长因子暂用动量替代
                + momentum["score"] * weights.get("成长", 20)
            ) / 100

            scored_stocks.append({
                "ts_code": ts_code,
                "total_score": round(total, 1),
                "factors": {
                    "valuation": valuation["score"],
                    "momentum": momentum["score"],
                    "technical": technical["score"],
                    "sentiment": sentiment["score"],
                },
                "factor_details": {
                    "valuation": valuation["details"],
                    "momentum": momentum["details"],
                    "technical": technical["details"],
                    "sentiment": sentiment["details"],
                },
            })

            print(f"  {ok} {ts_code}: {total:.1f}分")
        except Exception as e:
            print(f"  {warn} {ts_code} 评分失败: {e}")
            result["errors"].append(f"{ts_code} 评分异常: {e}")

    # 5. 排序取 Top N
    scored_stocks.sort(key=lambda x: x["total_score"], reverse=True)
    top_stocks = scored_stocks[:top_n]
    result["ranked_stocks"] = top_stocks

    # 6. 输出摘要
    print(f"\n  {'='*40}")
    print(f"  选股结果 Top {top_n}")
    print(f"  {'='*40}")
    for i, s in enumerate(top_stocks, 1):
        print(f"  {i}. {s['ts_code']} — {s['total_score']}分")
        print(f"     估值:{s['factors']['valuation']} 动量:{s['factors']['momentum']} 技术:{s['factors']['technical']} 情绪:{s['factors']['sentiment']}")

    return result


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="多因子选股")
    parser.add_argument("--top-n", type=int, default=5, help="输出候选数量")
    parser.add_argument("--force-refresh", action="store_true", help="强制刷新数据")
    args = parser.parse_args()

    report = generate_stock_picks(args.top_n, args.force_refresh)

    print("\n=== STOCK_PICKS ===")
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    print("=== END ===")

    # 保存报告
    output_dir = os.path.join(os.path.dirname(__file__), "..", "..", "data", "raw")
    os.makedirs(output_dir, exist_ok=True)
    today = datetime.now().strftime("%Y%m%d")
    output_path = os.path.join(output_dir, f"选股原始数据_{today}.json")
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n选股数据已保存: {output_path}")
