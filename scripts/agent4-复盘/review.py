"""
Agent4 复盘师 - 复盘核心脚本

功能：
1. 读取当天的情报报告 + 分析报告 + 风控报告
2. 获取当天实际行情数据
3. 对比预测 vs 实际，计算准确率
4. 分析偏差原因
5. 更新知识库（复盘记录 + 策略建议）

用法：
    source venv/Scripts/activate
    python -X utf8 scripts/agent4-复盘/review.py [YYYYMMDD]

如果省略日期，默认使用最新有报告的交易日。
"""

import os
import sys
import json
import re
import glob
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from scripts.utils.tushare_client import pro

# ======== 路径常量 ========
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def p(path: str) -> str:
    """转为项目绝对路径"""
    return os.path.join(PROJECT_ROOT, path)


def read_report(path: str) -> str:
    """读取报告文件"""
    full_path = p(path)
    if not os.path.exists(full_path):
        return ""
    with open(full_path, "r", encoding="utf-8") as f:
        return f.read()


def extract_predictions(analysis_text: str) -> dict:
    """从分析报告中提取预测/判断"""
    preds = {
        "market_定性": "",
        "env_score": None,
        "bullish_sectors": [],
        "bearish_sectors": [],
        "signals": [],
        "risk_warnings": [],
    }

    if not analysis_text:
        return preds

    # 提取大盘定性
    for line in analysis_text.split("\n"):
        if "定性" in line:
            preds["market_定性"] = line.strip()
        if "环境评分" in line:
            m = re.search(r"(\d+)/100", line)
            if m:
                preds["env_score"] = int(m.group(1))

    # 提取看涨板块（🔥 领涨板块 / ✅ 强烈信号）
    in_sector_section = False
    for line in analysis_text.split("\n"):
        # 领涨板块下面的表格行
        if "| 1 " in line and "板块" in line:
            parts = [p.strip() for p in line.split("|")]
            if len(parts) >= 3:
                sector = parts[2].strip()
                if sector and sector not in preds["bullish_sectors"]:
                    preds["bullish_sectors"].append(sector)

        # 强烈信号
        if "强烈信号" in line:
            in_sector_section = True
        if in_sector_section and "|" in line and "**" in line:
            parts = [p.strip() for p in line.split("|")]
            if len(parts) >= 3:
                sector = parts[2].strip().strip("**")
                if sector and sector not in preds["bullish_sectors"]:
                    preds["bullish_sectors"].append(sector)
        if in_sector_section and "关注信号" in line:
            in_sector_section = False

        # 看跌板块 / 风险提示
        if "回避" in line.lower():
            preds["bearish_sectors"].append(line.strip())

    # 领跌板块（表格行，排除表头和噪声）
    in_loser = False
    for line in analysis_text.split("\n"):
        if "领跌板块" in line and "###" in line:
            in_loser = True
            continue
        if in_loser and line.strip().startswith("|") and "---" not in line:
            parts = [p.strip() for p in line.split("|")]
            if len(parts) >= 3:
                sector = parts[2].strip()
                # 排除表头、个股名称、噪声
                skip_words = ["板块", "涨幅", "跌幅", "排名", "---------", "联讯", "宁德", "立讯", "新华"]
                if sector and not any(s in sector for s in skip_words):
                    if sector not in preds["bearish_sectors"]:
                        preds["bearish_sectors"].append(sector)
        if in_loser and ("异动板块" in line or "###" in line) and "领跌" not in line:
            break

    return preds


def fetch_actual_data(trade_date: str) -> dict:
    """获取当天的实际市场数据"""
    actual = {
        "date": trade_date,
        "indices": {},
        "sector_performance": {},
        "summary": "",
    }

    # 获取主要指数实际涨跌
    index_codes = {
        "上证指数": "000001.SH",
        "深证成指": "399001.SZ",
        "创业板指": "399006.SZ",
        "科创50": "000688.SH",
    }
    for name, code in index_codes.items():
        try:
            df = pro.index_daily(ts_code=code, start_date=trade_date, end_date=trade_date)
            if not df.empty:
                actual["indices"][name] = {
                    "close": float(df.iloc[0]["close"]),
                    "pct_chg": float(df.iloc[0]["pct_chg"]),
                }
        except:
            pass

    # 涨跌家数
    try:
        df = pro.daily(trade_date=trade_date)
        if not df.empty:
            up = int((df["pct_chg"] > 0).sum())
            down = int((df["pct_chg"] < 0).sum())
            actual["up_down"] = {"up": up, "down": down, "total": len(df)}
            up_ratio = up / len(df) * 100
            if up_ratio < 20:
                actual["summary"] = "市场极弱"
            elif up_ratio < 40:
                actual["summary"] = "市场偏弱"
            elif up_ratio < 60:
                actual["summary"] = "市场震荡"
            else:
                actual["summary"] = "市场偏强"
    except:
        pass

    return actual


def compare_predictions(predictions: dict, actual: dict, trade_date: str) -> dict:
    """对比预测 vs 实际"""
    review = {
        "date": trade_date,
        "market_定性": {"predicted": predictions.get("market_定性", ""), "actual": actual.get("summary", ""), "correct": None},
        "env_score": {"predicted": predictions.get("env_score"), "actual": None, "偏差": None},
        "sector_accuracy": {"correct": 0, "wrong": 0, "total": 0, "details": []},
        "大盘方向": {"correct": 0, "wrong": 0, "details": []},
    }

    # 1. 对比大盘方向
    for name, idx_data in actual.get("indices", {}).items():
        actual_pct = idx_data.get("pct_chg", 0)
        review["大盘方向"]["details"].append({
            "index": name,
            "pct_chg": round(actual_pct, 2),
            "direction": "涨" if actual_pct > 0 else ("跌" if actual_pct < 0 else "平"),
        })

    # 2. 对比板块预测（看涨板块是否确实涨了）
    # 由于我们没有实时板块涨跌数据，这里做逻辑推断
    # 主要依赖情报中的板块表现数据 + Tushare 日线
    for sector in predictions.get("bullish_sectors", []):
        review["sector_accuracy"]["details"].append({
            "sector": sector,
            "prediction": "看涨",
            "actual": "待确认（需结合当日行情判断）",
            "correct": None,
        })
        review["sector_accuracy"]["total"] += 1

    for sector in predictions.get("bearish_sectors", []):
        if sector in ["板块", "涨幅参考", "技术信号"]:
            continue
        review["sector_accuracy"]["details"].append({
            "sector": sector,
            "prediction": "看跌",
            "actual": "待确认",
            "correct": None,
        })
        review["sector_accuracy"]["total"] += 1

    # 3. 环境评分对比（复盘时重新计算一个简版）
    # 简化：用实际涨跌家数比例来估算
    ud = actual.get("up_down", {})
    total = ud.get("total", 0)
    if total > 0:
        up_ratio = ud.get("up", 0) / total
        if up_ratio > 0.6:
            actual_score = 85
        elif up_ratio > 0.4:
            actual_score = 60
        elif up_ratio > 0.2:
            actual_score = 40
        else:
            actual_score = 25
        review["env_score"]["actual"] = actual_score
        if predictions.get("env_score"):
            review["env_score"]["偏差"] = actual_score - predictions["env_score"]
            # 环境评分是中期趋势评估，单日数据只能反映短期
            # 偏差仅供参考，不作为正确/错误判定
            review["env_score"]["note"] = "预测为中期趋势评分，实际为单日情绪评分，两者维度不同"

    return review


def load_history(trade_date: str) -> list:
    """读取历史复盘记录"""
    pattern = p(f"knowledge/复盘记录/复盘_*.json")
    files = sorted(glob.glob(pattern), reverse=True)
    history = []
    for f in files[:30]:
        try:
            with open(f, "r", encoding="utf-8") as fp:
                history.append(json.load(fp))
        except:
            pass
    return history


def calculate_accuracy_trend(history: list) -> dict:
    """计算准确率趋势"""
    if not history:
        return {"total_days": 0, "avg_accuracy": 0, "trend": "暂无数据"}

    scores = []
    for h in history:
        acc = h.get("accuracy", {})
        板块准确率 = acc.get("sector_accuracy_rate")
        if 板块准确率 is not None:
            scores.append(板块准确率)

    if not scores:
        return {"total_days": len(history), "avg_accuracy": 0, "trend": "暂无数据"}

    avg = sum(scores) / len(scores)
    # 最近3天趋势
    recent = scores[-3:] if len(scores) >= 3 else scores
    recent_avg = sum(recent) / len(recent)

    if recent_avg > avg + 5:
        trend = "上升趋势 ↑"
    elif recent_avg < avg - 5:
        trend = "下降趋势 ↓"
    else:
        trend = "持平 →"

    return {
        "total_days": len(history),
        "avg_accuracy": round(avg, 1),
        "recent_avg": round(recent_avg, 1),
        "trend": trend,
    }


def update_knowledge(review_data: dict, trade_date: str):
    """更新知识库"""
    复盘记录_dir = p("knowledge/复盘记录")
    os.makedirs(复盘记录_dir, exist_ok=True)

    # 保存复盘记录 JSON
    record_path = os.path.join(复盘记录_dir, f"复盘_{trade_date}.json")
    with open(record_path, "w", encoding="utf-8") as f:
        json.dump(review_data, f, ensure_ascii=False, indent=2, default=str)

    return record_path


def generate_review_report(trade_date: str = None) -> dict:
    """主函数：生成完整的复盘报告"""
    if trade_date is None:
        # 自动寻找最新的分析报告
        reports_pattern = p("reports/日报/分析/分析报告_*.md")
        files = sorted(glob.glob(reports_pattern), reverse=True)
        if not files:
            # 用今天的日期
            trade_date = datetime.now().strftime("%Y%m%d")
        else:
            # 从文件名提取日期
            m = re.search(r"(\d{4}-\d{2}-\d{2})", files[0])
            if m:
                trade_date = m.group(1).replace("-", "")
            else:
                trade_date = datetime.now().strftime("%Y%m%d")

    日期显示 = f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:]}"

    print(f"[复盘师] 复盘日期: {日期显示}")
    _ok = "[OK]"
    _fail = "[FAIL]"

    review = {
        "date": 日期显示,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "inputs": {"情报": False, "分析": False, "风控": False},
        "predictions": {},
        "actual": {},
        "comparison": {},
        "accuracy": {"大盘方向": {}, "板块预测": {}, "综合准确率": 0},
        "偏差分析": [],
        "策略建议": [],
        "errors": [],
    }

    # 1. 读取输入报告
    情报 = read_report(f"reports/日报/情报/情报摘要_{日期显示}.md")
    分析 = read_report(f"reports/日报/分析/分析报告_{日期显示}.md")
    风控 = read_report(f"reports/日报/风控/风控报告_{日期显示}.md")

    if 情报:
        review["inputs"]["情报"] = True
        print(f"  {_ok} 情报报告已读取")
    if 分析:
        review["inputs"]["分析"] = True
        print(f"  {_ok} 分析报告已读取")
    if 风控:
        review["inputs"]["风控"] = True
        print(f"  {_ok} 风控报告已读取")

    # 2. 提取预测
    predictions = extract_predictions(分析)
    review["predictions"] = predictions
    print(f"  {_ok} 已提取预测: 看涨{len(predictions['bullish_sectors'])}个板块, 看跌{len(predictions['bearish_sectors'])}个板块")

    # 3. 获取实际数据
    try:
        actual = fetch_actual_data(trade_date)
        review["actual"] = actual
        print(f"  {_ok} 实际行情已获取: {len(actual['indices'])} 个指数")
    except Exception as e:
        review["errors"].append(f"实际行情获取失败: {e}")
        print(f"  {_fail} 实际行情: {e}")

    # 4. 对比分析
    comparison = compare_predictions(predictions, actual, trade_date)
    review["comparison"] = comparison

    # 5. 计算准确率
    # 大盘方向：如果有涨跌预测（结构市=偏震荡，准确率中等）
    大盘正确 = comparison.get("大盘方向", {}).get("correct", 0)
    大盘总 = len(comparison.get("大盘方向", {}).get("details", []))
    review["accuracy"]["大盘方向"] = {"correct": 大盘正确, "total": 大盘总, "rate": round(大盘正确/大盘总*100, 1) if 大盘总 > 0 else 0}

    # 板块预测准确率（需人工复核后填入）
    板块总 = comparison.get("sector_accuracy", {}).get("total", 0)
    review["accuracy"]["板块预测"] = {"total": 板块总, "待复核": 板块总, "note": "需人工复核板块实际涨跌后填入"}

    # 综合准确率 = 平均
    review["accuracy"]["综合准确率"] = round(review["accuracy"]["大盘方向"]["rate"] * 0.6, 1)

    # 6. 偏差分析
    env = comparison.get("env_score", {})
    if env.get("correct") is False:
        review["偏差分析"].append({
            "item": "环境评分偏差",
            "预测值": env.get("predicted"),
            "实际值": env.get("actual"),
            "偏差": env.get("偏差"),
            "可能原因": "待分析",
            "改进方向": "待定",
        })

    # 读取历史复盘，计算准确率趋势
    history = load_history(trade_date)
    trend = calculate_accuracy_trend(history)
    review["accuracy_trend"] = trend

    # 7. 生成策略建议
    if trend["total_days"] > 0:
        if "下降" in trend.get("trend", ""):
            review["策略建议"].append("准确率持续下降，建议：① 检查技术指标参数是否需要调整 ② 增加信息来源 ③ 减少预测频率")
        elif "上升" in trend.get("trend", ""):
            review["策略建议"].append("准确率在提升，继续保持当前分析框架")
        else:
            review["策略建议"].append("准确率稳定，可尝试增加新的分析维度")

    # 8. 更新知识库
    try:
        record_path = update_knowledge(review, trade_date.replace("-", ""))
        print(f"  {_ok} 复盘记录已保存: {record_path}")
    except Exception as e:
        review["errors"].append(f"知识库更新失败: {e}")
        print(f"  {_fail} 知识库更新: {e}")

    return review


if __name__ == "__main__":
    date_arg = sys.argv[1] if len(sys.argv) > 1 else None
    review = generate_review_report(date_arg)

    print("\n=== REVIEW_REPORT ===")
    print(json.dumps(review, ensure_ascii=False, indent=2, default=str))
    print("=== END ===")

    # 保存到 reports
    日期显示 = review["date"]
    output_dir = p(f"reports/日报/复盘")
    os.makedirs(output_dir, exist_ok=True)
    output_path = os.path.join(output_dir, f"复盘报告_{日期显示}.json")
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(review, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n复盘数据已保存: {output_path}")
