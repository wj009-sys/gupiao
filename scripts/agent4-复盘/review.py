"""
Agent4 复盘师 - 复盘核心脚本

功能：
1. 读取当天情报 + 分析 + 风控 + 选股建议 + 交易计划 报告
2. 获取当天实际行情数据
3. 对比预测 vs 实际，计算准确率
4. 复盘选股机器人推荐准确率和因子表现
5. 复盘操盘手交易计划的执行效果
6. 分析偏差原因
7. 更新知识库（复盘记录 + 策略建议 + 选股因子调整）

用法：
    source venv/Scripts/activate
    python -X utf8 scripts/agent4-复盘/review.py [YYYYMMDD]

如果省略日期，默认使用最新有报告的交易日。

D9反例（工作反例）：
1. 不要只报喜不报忧——亏损和失误比盈利更有复盘价值
2. 不要流于表面的偏差分析——每个偏差必须追溯到根因（Feynman三层：表象→深层→根因）
3. 不要过频修改策略框架——连续3天以上偏差才考虑策略调整，单日偏差只是随机波动
4. 不要忽视选股机器人的偏差——推荐票的涨跌反馈是因子调整的关键依据

D3异常处理表：
| 触发条件 | 一线修复 | 仍失败兜底 |
|---------|---------|-----------|
| 无当日报告（首个交易日） | 打印"首次运行，跳过当日复盘" | 只输出初始复盘记录，不更新知识库 |
| 行情数据获取失败 | 用报告中的预测价代替实际价 | 该偏差项标注"无行情数据，用预测价估算" |
| 知识库文件不存在（首次复盘） | 创建空白知识库文件 | 初始化"首次复盘"结构 |
| 选股原始数据缺少verified字段 | 用stock_picks中的scored列表推断 | 使用简化方法：涨跌>0为正确，<0为错误 |
| 因子表现计算除零（无验证数据） | 因子表现设为空字典 | 跳过因子调整建议章节 |
| 知识库写入失败（磁盘/权限） | 重试1次 | 输出错误，建议手动检查知识库目录权限 |

D4 CHECKPOINT:
- CP1-偏差数据验证：每个偏差必须有"预测方向/实际方向/偏差原因"三条信息
- CP2-知识库更新确认：知识库文件必须实际更新（检查mtime或写入内容）
- CP3-因子调整前提：连续3日以上偏差才建议调整因子权重
- CP4-改进措施：每个偏差必须附带改进措施
- CP5-准确率计算：总预测数/正确数/准确率必须有明确的统计口径
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
        except Exception:
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
    except Exception:
        pass

    # 板块实际表现（用于验证板块预测）
    try:
        ths_df = pro.ths_daily(trade_date=trade_date)
        if ths_df is not None and not ths_df.empty:
            actual["sector_performance"] = {}
            for _, row in ths_df.iterrows():
                name = row.get("name", "")
                pct = row.get("pct_chg", 0)
                if name:
                    actual["sector_performance"][name] = round(float(pct), 2)
    except Exception:
        pass

    return actual


def load_stock_picker_factors(trade_date: str) -> dict:
    """从选股原始数据JSON中加载各因子独立评分"""
    factor_map = {}
    raw_dir = os.path.join(os.path.dirname(__file__), "..", "..", "data", "raw")
    # 匹配当天所有模式的选股原始数据
    pattern = os.path.join(raw_dir, f"选股原始数据_{trade_date}_*.json")
    files = sorted(glob.glob(pattern), reverse=True)
    for fp in files:
        try:
            with open(fp, "r", encoding="utf-8") as f:
                data = json.load(f)
            stocks = data.get("stock_picks", {}).get("stocks", [])
            if not stocks:
                stocks = data.get("top_stocks", [])
            for s in stocks:
                code = s.get("ts_code", s.get("code", ""))
                factors = s.get("factors", {})
                if code and factors:
                    # 映射中英文因子名
                    name_map = {
                        "估值": ["估值", "valuation", "value"],
                        "成长": ["成长", "growth"],
                        "动量": ["动量", "momentum", "momo"],
                        "技术面": ["技术面", "技术", "technical"],
                        "情绪": ["情绪", "sentiment"],
                    }
                    result = {}
                    for cn_name, aliases in name_map.items():
                        for alias in aliases:
                            if alias in factors:
                                result[cn_name] = factors[alias]
                                break
                    if result:
                        factor_map[code] = result
        except Exception:
            continue
    return factor_map


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
    sector_perf = actual.get("sector_performance", {})
    has_real_data = bool(sector_perf)

    def lookup_sector_performance(sector_name: str) -> float:
        """在板块表现数据中模糊匹配板块名称"""
        for s_name, s_pct in sector_perf.items():
            if sector_name in s_name or s_name in sector_name:
                return s_pct
        return None

    for sector in predictions.get("bullish_sectors", []):
        actual_pct = lookup_sector_performance(sector)
        if actual_pct is not None:
            is_correct = actual_pct > 0
            review["sector_accuracy"]["details"].append({
                "sector": sector,
                "prediction": "看涨",
                "actual": f"{actual_pct:+.2f}%" if actual_pct else "无数据",
                "correct": is_correct,
            })
            if is_correct:
                review["sector_accuracy"]["correct"] += 1
            else:
                review["sector_accuracy"]["wrong"] += 1
        else:
            review["sector_accuracy"]["details"].append({
                "sector": sector,
                "prediction": "看涨",
                "actual": "待确认（无板块数据）",
                "correct": None,
            })
        review["sector_accuracy"]["total"] += 1

    for sector in predictions.get("bearish_sectors", []):
        if sector in ["板块", "涨幅参考", "技术信号"]:
            continue
        actual_pct = lookup_sector_performance(sector)
        if actual_pct is not None:
            is_correct = actual_pct < 0
            review["sector_accuracy"]["details"].append({
                "sector": sector,
                "prediction": "看跌",
                "actual": f"{actual_pct:+.2f}%" if actual_pct else "无数据",
                "correct": is_correct,
            })
            if is_correct:
                review["sector_accuracy"]["correct"] += 1
            else:
                review["sector_accuracy"]["wrong"] += 1
        else:
            review["sector_accuracy"]["details"].append({
                "sector": sector,
                "prediction": "看跌",
                "actual": "待确认（无板块数据）",
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
        except Exception:
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


def extract_stock_picks(pick_text: str) -> dict:
    """从选股建议报告中提取推荐的候选股票"""
    picks = {"stocks": [], "total": 0}
    if not pick_text:
        return picks

    # 匹配股票代码 + 评分模式
    stock_patterns = [
        r'\*\*(\w+)\((\d{6}\.(?:SZ|SH))\)\*\*.*?[：:]\s*(\d+)分',
        r'(\w+)\((\d{6}\.(?:SZ|SH))\).*?(\d+)分',
    ]
    for pattern in stock_patterns:
        for m in re.finditer(pattern, pick_text, re.DOTALL):
            picks["stocks"].append({
                "name": m.group(1).strip(),
                "code": m.group(2),
                "score": int(m.group(3)),
            })
            picks["total"] += 1

    # 提取否决案例
    veto_lines = [l for l in pick_text.split('\n') if '否决' in l and '~~' in l]
    picks["vetoed"] = []
    for vl in veto_lines:
        code_match = re.search(r'(\d{6}\.(SZ|SH))', vl)
        reason_match = re.search(r'否决.*?([^。]+)', vl)
        if code_match:
            picks["vetoed"].append({
                "code": code_match.group(0),
                "reason": reason_match.group(1) if reason_match else "未说明",
            })

    return picks


def extract_trade_plan(trade_text: str) -> dict:
    """从交易计划报告中提取买入/卖出/持有清单"""
    plan = {"buy": [], "sell": [], "hold": []}
    if not trade_text:
        return plan

    # 提取买入清单
    in_buy = False
    for line in trade_text.split('\n'):
        stripped = line.strip()
        if '买入清单' in stripped or '### 买入' in stripped:
            in_buy = True
            continue
        if '卖出清单' in stripped or '### 卖出' in stripped:
            in_buy = False
        if in_buy and stripped.startswith('|') and '---' not in stripped:
            parts = [p.strip() for p in stripped.split('|') if p.strip()]
            if len(parts) >= 2:
                code_match = re.search(r'(\d{6}\.(SZ|SH))', stripped)
                price_match = re.search(r'(\d+\.?\d*)\s*-\s*(\d+\.?\d*)', stripped)
                if code_match:
                    plan["buy"].append({
                        "code": code_match.group(0),
                        "price_range": f"{price_match.group(1)}-{price_match.group(2)}" if price_match else "",
                    })

    # 提取卖出清单
    in_sell = False
    for line in trade_text.split('\n'):
        stripped = line.strip()
        if '卖出清单' in stripped or '### 卖出' in stripped:
            in_sell = True
            continue
        if '持有清单' in stripped or '### 持有' in stripped or '### 仓位' in stripped:
            in_sell = False
        if in_sell and stripped.startswith('|') and '---' not in stripped:
            code_match = re.search(r'(\d{6}\.(SZ|SH))', stripped)
            if code_match:
                plan["sell"].append({"code": code_match.group(0)})

    return plan


def fetch_real_prices(stocks: list, trade_date: str) -> list:
    """获取候选股票的实际行情来验证选股评分"""
    verified = []
    for s in stocks[:10]:  # 最多验证10只
        try:
            code = s.get("code", "")
            if not code:
                continue
            df = pro.daily(ts_code=code, start_date=trade_date, end_date=trade_date)
            if df is not None and not df.empty:
                close = float(df.iloc[0]["close"])
                pct_chg = float(df.iloc[0].get("pct_chg", 0))
                s["actual_close"] = close
                s["actual_pct"] = round(pct_chg, 2)
                # 简单判断：上涨=推荐正确，下跌=推荐需审视
                s["verdict"] = "correct" if pct_chg > 0 else ("wrong" if pct_chg < -2 else "neutral")
                verified.append(s)
            else:
                s["actual_close"] = None
                s["verdict"] = "no_data"
                verified.append(s)
        except Exception:
            s["verdict"] = "error"
            verified.append(s)
    return verified


def update_knowledge(review_data: dict, trade_date: str) -> str:
    """更新知识库（复盘记录 + 策略建议）"""
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
    ok = "[OK]"
    fail = "[FAIL]"

    review = {
        "date": 日期显示,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "inputs": {"情报": False, "分析": False, "风控": False, "选股": False, "操盘": False},
        "predictions": {},
        "stock_picks": {"stocks": [], "verified": [], "total": 0},
        "trade_plan": {"buy": [], "sell": [], "hold": []},
        "actual": {},
        "comparison": {},
        "accuracy": {"大盘方向": {}, "板块预测": {}, "选股准确率": {}, "综合准确率": 0},
        "偏差分析": [],
        "策略建议": [],
        "因子建议": [],
        "errors": [],
    }

    # 1. 读取输入报告（全部7个Agent）
    情报 = read_report(f"reports/日报/情报/情报摘要_{日期显示}.md")
    分析 = read_report(f"reports/日报/分析/分析报告_{日期显示}.md")
    风控 = read_report(f"reports/日报/风控/风控报告_{日期显示}.md")
    选股 = read_report(f"reports/日报/选股/选股建议_{日期显示}.md")
    操盘 = read_report(f"reports/日报/操盘/交易计划_{日期显示}.md")

    if 情报:
        review["inputs"]["情报"] = True
        print(f"  {ok} 情报报告已读取")
    if 分析:
        review["inputs"]["分析"] = True
        print(f"  {ok} 分析报告已读取")
    if 风控:
        review["inputs"]["风控"] = True
        print(f"  {ok} 风控报告已读取")
    if 选股:
        review["inputs"]["选股"] = True
        print(f"  {ok} 选股建议已读取")
    if 操盘:
        review["inputs"]["操盘"] = True
        print(f"  {ok} 交易计划已读取")

    # 2. 提取预测
    predictions = extract_predictions(分析)
    review["predictions"] = predictions
    print(f"  {ok} 已提取预测: 看涨{len(predictions['bullish_sectors'])}个板块, 看跌{len(predictions['bearish_sectors'])}个板块")

    # 3. 获取实际数据
    try:
        actual = fetch_actual_data(trade_date)
        review["actual"] = actual
        print(f"  {ok} 实际行情已获取: {len(actual['indices'])} 个指数")
    except Exception as e:
        review["errors"].append(f"实际行情获取失败: {e}")
        print(f"  {fail} 实际行情: {e}")

    # 4. 对比分析
    comparison = compare_predictions(predictions, actual, trade_date)
    review["comparison"] = comparison

    # 4b. 选股机器人复盘
    if 选股:
        stock_picks = extract_stock_picks(选股)
        review["stock_picks"]["stocks"] = stock_picks.get("stocks", [])
        review["stock_picks"]["total"] = stock_picks.get("total", 0)
        review["stock_picks"]["vetoed"] = stock_picks.get("vetoed", [])
        # 验证候选股票实际表现
        if stock_picks["stocks"]:
            verified = fetch_real_prices(stock_picks["stocks"], trade_date)
            review["stock_picks"]["verified"] = verified
            correct_count = sum(1 for v in verified if v.get("verdict") == "correct")
            wrong_count = sum(1 for v in verified if v.get("verdict") == "wrong")
            total_verified = len(verified)
            review["accuracy"]["选股准确率"] = {
                "correct": correct_count,
                "wrong": wrong_count,
                "total": total_verified,
                "rate": round(correct_count / total_verified * 100, 1) if total_verified > 0 else 0,
            }
            print(f"  {ok} 选股验证: {correct_count}/{total_verified} 只上涨")
            # 因子表现排行（从原始数据中提取各因子独立评分）
            factor_data = load_stock_picker_factors(trade_date)
            factor_performance = {
                "估值": {"correct": 0, "wrong": 0, "total": 0, "avg_score": 0},
                "成长": {"correct": 0, "wrong": 0, "total": 0, "avg_score": 0},
                "动量": {"correct": 0, "wrong": 0, "total": 0, "avg_score": 0},
                "技术面": {"correct": 0, "wrong": 0, "total": 0, "avg_score": 0},
                "情绪": {"correct": 0, "wrong": 0, "total": 0, "avg_score": 0},
            }
            verified_codes = {v.get("code", ""): v for v in verified}
            for s in stock_picks.get("stocks", []):
                code = s.get("code", "")
                v = verified_codes.get(code)
                if v and v.get("verdict") in ("correct", "wrong"):
                    is_correct = v.get("verdict") == "correct"
                    # 使用原始数据中的各因子独立评分
                    factor_scores = factor_data.get(code, {})
                    has_factors = len(factor_scores) >= 4
                    for fname in factor_performance:
                        factor_performance[fname]["total"] += 1
                        if has_factors:
                            fscore = factor_scores.get(fname, 50)
                        else:
                            # 无因子分时，用总分推断
                            total_score = s.get("score", 50)
                            fscore = total_score
                        if (is_correct and fscore >= 80) or (not is_correct and fscore < 60):
                            factor_performance[fname]["correct"] += 1
                        else:
                            factor_performance[fname]["wrong"] += 1
                        factor_performance[fname]["avg_score"] += fscore

            # 计算准确率
            for fname, data in factor_performance.items():
                if data["total"] > 0:
                    data["avg_score"] = round(data["avg_score"] / data["total"], 1)
                    data["accuracy"] = round(data["correct"] / data["total"] * 100, 1)
                else:
                    data["accuracy"] = 0
            review["因子表现"] = factor_performance

            # 因子调整建议
            if wrong_count > correct_count and total_verified >= 3:
                review["因子建议"].append("推荐票多数下跌，建议检查选股因子权重是否需调整")
            for fname, data in factor_performance.items():
                if data["total"] >= 3 and data["accuracy"] < 40:
                    review["因子建议"].append(f"{fname}因子准确率{data['accuracy']}%（{data['correct']}/{data['total']}），建议下调权重")
                elif data["total"] >= 3 and data["accuracy"] > 75:
                    review["因子建议"].append(f"{fname}因子准确率{data['accuracy']}%（{data['correct']}/{data['total']}），表现良好可维持权重")

    # 4c. 操盘手复盘
    if 操盘:
        trade_plan = extract_trade_plan(操盘)
        review["trade_plan"]["buy"] = trade_plan.get("buy", [])
        review["trade_plan"]["sell"] = trade_plan.get("sell", [])
        review["trade_plan"]["hold"] = trade_plan.get("hold", [])
        if trade_plan["buy"] or trade_plan["sell"]:
            print(f"  {ok} 交易计划已提取: {len(trade_plan['buy'])}买入 {len(trade_plan['sell'])}卖出")

    # 5. 计算准确率
    # 大盘方向：如果有涨跌预测（结构市=偏震荡，准确率中等）
    大盘正确 = comparison.get("大盘方向", {}).get("correct", 0)
    大盘总 = len(comparison.get("大盘方向", {}).get("details", []))
    review["accuracy"]["大盘方向"] = {"correct": 大盘正确, "total": 大盘总, "rate": round(大盘正确/大盘总*100, 1) if 大盘总 > 0 else 100}

    # 板块预测准确率（自动从对比数据计算）
    板块正确 = comparison.get("sector_accuracy", {}).get("correct", 0)
    板块错误 = comparison.get("sector_accuracy", {}).get("wrong", 0)
    板块总 = comparison.get("sector_accuracy", {}).get("total", 0)
    板块已校验 = 板块正确 + 板块错误
    review["accuracy"]["板块预测"] = {
        "correct": 板块正确,
        "wrong": 板块错误,
        "total": 板块总,
        "verified": 板块已校验,
        "rate": round(板块正确 / 板块已校验 * 100, 1) if 板块已校验 > 0 else 0,
        "pending_review": 板块总 - 板块已校验,
        "note": f"已自动校验{板块已校验}/{板块总}个板块预测，其余需人工复核" if 板块已校验 < 板块总 else "所有板块预测已自动校验",
    }

    # 综合准确率 = 板块 * 0.6 + 大盘 * 0.4
    大盘率 = review["accuracy"]["大盘方向"]["rate"]
    板块率 = review["accuracy"]["板块预测"]["rate"]
    review["accuracy"]["综合准确率"] = round(板块率 * 0.6 + 大盘率 * 0.4, 1)

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
        print(f"  {ok} 复盘记录已保存: {record_path}")
    except Exception as e:
        review["errors"].append(f"知识库更新失败: {e}")
        print(f"  {fail} 知识库更新: {e}")

    return review


if __name__ == "__main__":
    date_arg = sys.argv[1] if len(sys.argv) > 1 else None
    review = generate_review_report(date_arg)

    print("\n=== RESULT_JSON ===")
    print(json.dumps(review, ensure_ascii=False, indent=2, default=str))
    print("=== END ===")

    # 保存到 data/raw（统一格式 YYYYMMDD）
    日期显示 = review["date"]
    raw_date = 日期显示.replace("-", "")
    output_dir = p("data/raw")
    os.makedirs(output_dir, exist_ok=True)
    output_path = os.path.join(output_dir, f"复盘报告_{raw_date}.json")
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(review, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n复盘数据已保存: {output_path}")
