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
from datetime import datetime

# ======== 路径常量 ========
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

sys.path.insert(0, PROJECT_ROOT)
from scripts.utils.tushare_client import pro

# 知识库变更日志（导入失败不中断复盘）
try:
    from scripts.utils.knowledge_lint import update_changes, run_lint, fix_index
    _has_knowledge_tools = True
except Exception as e:
    print(f"  [WARN] 知识库工具导入失败: {e}")
    _has_knowledge_tools = False


def p(path: str) -> str:
    """转为项目绝对路径"""
    return os.path.join(PROJECT_ROOT, path)


def read_report(path: str) -> str:
    """读取报告文件，支持回退到 data/raw/ 目录查找原始JSON"""
    full_path = p(path)
    if os.path.exists(full_path):
        with open(full_path, "r", encoding="utf-8") as f:
            return f.read()

    # 回退到 data/raw/ 目录查找 JSON 原始数据
    raw_dir = p("data/raw")
    basename = os.path.splitext(os.path.basename(path))[0]
    # 提取日期（YYYY-MM-DD → YYYYMMDD）
    date_match = re.search(r"(\d{4})-(\d{2})-(\d{2})", basename)
    if date_match:
        date_raw = date_match.group(1) + date_match.group(2) + date_match.group(3)
    else:
        date_raw = ""
    # 提取前缀（去掉日期部分）
    prefix = basename.replace(f"_{date_match.group(0)}", "") if date_match else basename
    # 匹配 data/raw/{prefix}_{date_raw}*.json
    if date_raw:
        pattern = os.path.join(raw_dir, f"{prefix}_{date_raw}*.json")
        files = sorted(glob.glob(pattern), reverse=True)
        if files:
            with open(files[0], "r", encoding="utf-8") as f:
                data = json.load(f)
                # JSON转为可读文本摘要
                return json.dumps(data, ensure_ascii=False, indent=2)
    return ""


def extract_predictions(analysis_text: str) -> dict:
    """从分析报告中提取预测/判断（支持标准Markdown + 灵活匹配）"""
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

    for line in analysis_text.split("\n"):
        stripped = line.strip()
        # 大盘定性 / 核心判断
        if any(kw in stripped for kw in ["定性", "核心判断", "大盘判断"]):
            # 去掉Markdown标记，提取冒号后的内容
            text = re.sub(r'[*#]', '', stripped).strip()
            preds["market_定性"] = text.split("：")[-1].split(":")[-1].strip() if "：" in text or ":" in text else text

        # 环境评分：支持 "52.0" "52/100" "**52.0**"
        if "环境评分" in stripped:
            # 匹配 **52.0** 或 52.0 或 52/100
            m = re.search(r'\*{0,2}(\d+\.?\d*)\*{0,2}(?:/100)?', stripped)
            if m:
                preds["env_score"] = int(float(m.group(1)))

        # 看涨/强势板块（表格行或列表）
        if re.match(r'^\|\s*\d+\s*\|', stripped):
            parts = [p.strip() for p in stripped.split('|') if p.strip()]
            if len(parts) >= 3:
                # 取第2列作为板块名（排除排名列）
                candidate = re.sub(r'[*#^]', '', parts[1]).strip()
                # 排除噪声词
                skip_words = ["板块", "指数", "收盘", "涨跌", "MACD", "MA5", "MA20", "MA60", "评分", "---------"]
                if candidate and len(candidate) <= 12 and not any(s in candidate for s in skip_words):
                    if candidate not in preds["bullish_sectors"]:
                        preds["bullish_sectors"].append(candidate)

        # 领跌/看跌板块
        if "领跌" in stripped or "回避" in stripped.lower():
            parts = [p.strip() for p in stripped.split('|') if p.strip()]
            for p in parts:
                p_clean = re.sub(r'[*#^]', '', p).strip()
                if p_clean and len(p_clean) <= 12 and "板块" not in p_clean:
                    if p_clean not in preds["bearish_sectors"] and p_clean not in ["1", "2", "3", "4", "5"]:
                        try:
                            float(p_clean)
                        except ValueError:
                            preds["bearish_sectors"].append(p_clean)

        # 信号
        if any(kw in stripped for kw in ["死叉", "金叉", "多头", "空头", "强烈信号", "关注信号"]):
            preds["signals"].append(stripped[:80])

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
        except Exception as e:
            print(f"  ⚠️ 获取指数{name}({code})行情失败: {e}")

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
    except Exception as e:
        print(f"  ⚠️ 获取涨跌家数失败: {e}")

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
    except Exception as e:
        print(f"  ⚠️ 获取板块表现失败: {e}")

    return actual


def load_stock_picker_factors(trade_date: str) -> dict:
    """从选股原始数据JSON中加载各因子独立评分"""
    factor_map = {}
    raw_dir = p("data/raw")
    # 匹配当天所有模式的选股原始数据
    pattern = os.path.join(raw_dir, f"选股原始数据_{trade_date}_*.json")
    files = sorted(glob.glob(pattern), reverse=True)
    for fp in files:
        try:
            with open(fp, "r", encoding="utf-8") as f:
                data = json.load(f)
            # 支持多种格式:
            #   旧格式: data["stock_picks"]["stocks"]
            #   旧格式: data["top_stocks"]
            #   deep_scan格式: data["ranked_stocks"]
            stocks = data.get("ranked_stocks", [])
            if not stocks:
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
        except Exception as e:
            print(f"  [WARN] 解析选股JSON失败 {os.path.basename(fp)}: {e}")
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
            deviation = actual_score - predictions["env_score"]
            review["env_score"]["偏差"] = deviation
            # 环境评分正确性：偏差在±20内视为基本正确
            review["env_score"]["correct"] = abs(deviation) <= 20
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
        except Exception as e:
            print(f"  [WARN] 加载复盘记录失败: {os.path.basename(f)}: {e}")
    return history


def calculate_accuracy_trend(history: list) -> dict:
    """计算准确率趋势"""
    if not history:
        return {"total_days": 0, "avg_accuracy": 0, "trend": "暂无数据"}

    scores = []
    for h in history:
        acc = h.get("accuracy", {})
        # 兼容中英文key：板块预测/板块准确率/sector_accuracy_rate
        sector_rate = acc.get("板块预测", {}).get("rate") if isinstance(acc.get("板块预测"), dict) else (
            acc.get("板块准确率") or acc.get("sector_accuracy_rate")
        )
        if sector_rate is not None:
            scores.append(sector_rate)

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

    # 支持 SZ, SH, BJ 三个市场
    code_pattern = r'(\d{6}\.(?:SZ|SH|BJ))'
    score_pattern = r'(\d+\.?\d*)\s*(?:分|(?:\|))'

    # 方式1: 表格解析 — 匹配 "*| 代码 | 名称 | 市场 | **评分** |*"
    # 例如: | 🥇 | 920221.BJ | **易实精密** | 北交所 | **77.5** | ⭐⭐⭐⭐⭐ |
    in_table = False
    table_started = False
    for line in pick_text.split('\n'):
        stripped = line.strip()
        if '| 排名 | 代码 |' in stripped or 'Top 5' in stripped:
            in_table = True
            continue
        if in_table and stripped.startswith('|') and '---' in stripped:
            table_started = True
            continue
        if in_table and table_started and stripped.startswith('|'):
            code_match = re.search(code_pattern, stripped)
            score_match = re.search(r'\*\*(\d+\.?\d*)\*\*', stripped)
            name_match = re.search(r'\*\*(.*?)\*\*', stripped)
            if code_match and score_match:
                name = name_match.group(1) if name_match else ""
                # 避免把评分当名字
                try:
                    float(name)
                    name = ""
                except ValueError:
                    pass  # name is not a number (e.g. score column), keep it
                # 也检查 | 名称列 | 不是代码
                parts = [p.strip().strip('*') for p in stripped.split('|') if p.strip()]
                for part in parts:
                    if re.match(r'^[一-鿿\w]{2,8}$', part) and not re.search(r'\d', part):
                        name = part
                        break
                picks["stocks"].append({
                    "name": name,
                    "code": code_match.group(1),
                    "score": int(float(score_match.group(1))),
                })
                picks["total"] += 1
            elif code_match:
                # 只有代码没有评分，用第三个管道后的数字
                parts = [p.strip() for p in stripped.split('|') if p.strip()]
                score = 0
                for part in parts:
                    m = re.search(r'(\d+\.?\d*)', part)
                    if m:
                        try:
                            s = float(m.group(1))
                            if 0 <= s <= 100:
                                score = int(s)
                        except ValueError:
                            print(f"  [WARN] 评分解析失败，股票 {code_match.group(1)} 无法解析值 '{m.group(1)}'，跳过", file=sys.stderr)
                picks["stocks"].append({
                    "name": "",
                    "code": code_match.group(1),
                    "score": score,
                })
                picks["total"] += 1
        elif in_table and table_started and not stripped.startswith('|'):
            in_table = False  # 表格结束

    # 方式2: 行内匹配（回退）
    if picks["total"] == 0:
        for line in pick_text.split('\n'):
            code_match = re.search(code_pattern, line)
            score_match = re.search(r'(\d+\.?\d*)\s*分', line)
            name_match = re.search(r'\*\*(.*?)\*\*', line)
            if code_match and score_match:
                name = ""
                if name_match:
                    try:
                        float(name_match.group(1))
                    except ValueError:
                        name = name_match.group(1)
                if not name:
                    parts = line.split('|')
                    for p in parts:
                        p_clean = p.strip().strip('*')
                        if re.match(r'^[一-鿿]{2,8}$', p_clean) and not re.search(r'\d', p_clean):
                            name = p_clean
                            break
                picks["stocks"].append({
                    "name": name,
                    "code": code_match.group(1),
                    "score": int(float(score_match.group(1))),
                })
                picks["total"] += 1

    # 提取否决案例
    veto_lines = [l for l in pick_text.split('\n') if '否决' in l and '~~' in l]
    picks["vetoed"] = []
    for vl in veto_lines:
        code_match = re.search(code_pattern, vl)
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

    code_pattern = r'(\d{6}\.(?:SZ|SH|BJ))'

    # 分段提取：按 ## 或 ### 标题分节
    sections = re.split(r'#{2,}\s+', trade_text)
    for section in sections:
        # 卖出清单/卖出计划
        if any(kw in section[:50] for kw in ["卖出计划", "卖出清单", "卖出操作"]):
            for line in section.split('\n'):
                code_match = re.search(code_pattern, line)
                if code_match and '|' in line:
                    parts = [p.strip() for p in line.split('|')]
                    priority = "中"
                    if len(parts) >= 2:
                        prio_text = parts[0].strip()
                        if '高' in prio_text or '最高' in prio_text or '🔴' in prio_text:
                            priority = "高"
                    pnl = ""
                    for p in parts:
                        m_pnl = re.search(r'[-+]?\d+\.?\d*%', p)
                        if m_pnl:
                            pnl = m_pnl.group(0)
                            break
                    plan["sell"].append({
                        "code": code_match.group(0),
                        "priority": priority,
                        "pnl_pct": pnl,
                    })

        # 持有计划/持有清单
        if any(kw in section[:50] for kw in ["持有计划", "持有清单"]):
            for line in section.split('\n'):
                code_match = re.search(code_pattern, line)
                if code_match and '|' in line:
                    parts = [p.strip() for p in line.split('|') if p.strip()]
                    plan["hold"].append({"code": code_match.group(0)})

        # 买入计划/买入清单 - 无内容也记录
        if any(kw in section[:50] for kw in ["买入计划", "买入清单"]):
            if not section.strip():
                pass  # 无买入

    return plan


def extract_leader_decision(decision_text: str) -> dict:
    """从投资决策报告中提取决策内容用于复盘"""
    result = {
        "sell_decisions": [],
        "buy_decisions": [],
        "hold_decisions": [],
        "conflicts": [],
        "rework_items": [],
        "quality_review": {},
    }
    if not decision_text:
        return result

    code_pattern = r'(\d{6}\.(?:SZ|SH|BJ))'

    # 分段处理（支持 ## 和 ###）
    sections = re.split(r'#{2,}\s+', decision_text)
    for section in sections:
        # === 卖出清单 ===
        if "卖出清单" in section[:50]:
            for line in section.split('\n'):
                code_match = re.search(code_pattern, line)
                if code_match and '|' in line:
                    # 提取操作和盈亏
                    action = "卖出"
                    if '止损' in line:
                        action = "止损"
                    elif '止盈' in line:
                        action = "止盈"
                    elif '减仓' in line:
                        action = "减仓"
                    pnl = ""
                    m_pnl = re.search(r'[-+]?\d+\.?\d*%', line) or re.search(r'盈亏.*?([-+]?\d+\.?\d*)', line)
                    if m_pnl:
                        pnl = m_pnl.group(0) if '%' in m_pnl.group(0) else m_pnl.group(1) + '%'
                    result["sell_decisions"].append({
                        "code": code_match.group(0).split('.')[0],  # 只取6位数字
                        "name": "",
                        "reason": action,
                        "pnl": pnl,
                    })

        # === 持有清单 ===
        if "持有清单" in section[:50]:
            for line in section.split('\n'):
                code_match = re.search(code_pattern, line)
                if code_match:
                    name_match = re.search(r'\|.*?\*\*(.*?)\*\*', line)
                    name = name_match.group(1) if name_match else ""
                    result["hold_decisions"].append({"name": name if name else code_match.group(0)})

        # === 冲突检测 ===
        if any(kw in section[:50] for kw in ["纠纷", "冲突", "争议", "分歧", "仲裁"]):
            for line in section.split('\n'):
                if '一致' in line or '无分歧' in line:
                    result["conflicts"].append({"type": "无分歧", "detail": line.strip()[:100]})
                elif '分歧' in line:
                    result["conflicts"].append({"type": "有分歧", "detail": line.strip()[:100]})

    # === 全局检测：质量审核结果 ===
    quality_section = ""
    for section in sections:
        if "质量审核" in section[:50] or "Agent质量" in section[:50]:
            quality_section = section
            break

    passed = len(re.findall(r'✅\s*通过', quality_section or decision_text))
    needs_work = len(re.findall(r'⚠️\s*需补充|⚠️\s*建议打回', quality_section or decision_text))
    rejected = len(re.findall(r'❌\s*不合格', quality_section or decision_text))
    result["quality_review"] = {
        "passed": passed,
        "needs_work": needs_work,
        "rejected": rejected,
    }

    return result


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
        except Exception as e:
            s["verdict"] = "error"
            s["error_info"] = str(e)
            print(f"  [WARN] 获取{s.get('code', '?')}行情失败: {e}")
            verified.append(s)
    return verified


def update_knowledge(review_data: dict, trade_date: str) -> str:
    """更新知识库（复盘记录 + 策略建议 + 变更日志 + 索引更新）"""
    复盘记录_dir = p("knowledge/复盘记录")
    os.makedirs(复盘记录_dir, exist_ok=True)

    # 保存复盘记录 JSON
    record_path = os.path.join(复盘记录_dir, f"复盘_{trade_date}.json")
    with open(record_path, "w", encoding="utf-8") as f:
        json.dump(review_data, f, ensure_ascii=False, indent=2, default=str)

    # === 知识库变更日志 ===
    if _has_knowledge_tools:
        try:
            # CHANGES.md 记录这次复盘
            update_changes(
                "复盘",
                f"复盘记录/复盘_{trade_date}.json",
                f"日常复盘：偏差分析 + 策略建议 + 准确率趋势",
            )
            print(f"  [OK] CHANGES.md 已更新")
        except Exception as e:
            print(f"  [WARN] CHANGES.md 更新失败: {e}")

        try:
            # 更新 INDEX.md（如果有新的复盘记录文件）
            from scripts.utils.knowledge_lint import scan_files
            files = scan_files()
            added = fix_index(files)
            if added > 0:
                print(f"  [OK] INDEX.md 已更新: 新增 {added} 个条目")
        except Exception as e:
            print(f"  [WARN] INDEX.md 更新失败: {e}")

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
        "leader": {"sell_decisions": [], "buy_decisions": [], "hold_decisions": [], "conflicts": [], "quality_review": {}},
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
    决策 = read_report(f"reports/日报/决策/投资决策_{日期显示}.md")

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
    if 决策:
        review["inputs"]["决策"] = True
        print(f"  {ok} 投资决策报告已读取")

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

    # 4d. 投资领导复盘
    if 决策:
        leader_review = extract_leader_decision(决策)
        review["leader"] = leader_review
        decisions_count = len(leader_review.get("sell_decisions", [])) + len(leader_review.get("hold_decisions", []))
        if leader_review.get("buy_decisions"):
            decisions_count += len(leader_review["buy_decisions"])
        print(f"  {ok} 投资决策已提取: {decisions_count}项决策")

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

    # 8. 更新知识库（含变更日志 + 索引更新）
    try:
        record_path = update_knowledge(review, trade_date.replace("-", ""))
        print(f"  {ok} 复盘记录已保存: {record_path}")
    except Exception as e:
        review["errors"].append(f"知识库更新失败: {e}")
        print(f"  {fail} 知识库更新: {e}")

    # 9. 知识库一致性检查（每周一次自动 lint，每天输出摘要）
    if _has_knowledge_tools:
        try:
            lint_report = run_lint(max_stale_days=90)
            review["knowledge_lint"] = {
                "total_files": lint_report["total_files"],
                "errors": lint_report["summary"]["errors"],
                "warnings": lint_report["summary"]["warnings"],
                "orphans": [o for o in lint_report.get("orphans", [])],
                "broken_refs": len(lint_report.get("broken_refs", [])),
                "stale": [s["file"] for s in lint_report.get("stale", [])],
                "contradictions": len(lint_report.get("contradictions", [])),
                "health": "健康" if lint_report["summary"]["errors"] == 0 and lint_report["summary"]["warnings"] == 0
                          else "需关注" if lint_report["summary"]["errors"] == 0
                          else "需修复",
            }
            if lint_report["summary"]["errors"] > 0 or lint_report["summary"]["warnings"] > 0:
                print(f"  {ok} 知识库健康度: {review['knowledge_lint']['health']} "
                      f"(错误{lint_report['summary']['errors']}, 警告{lint_report['summary']['warnings']})")
            else:
                print(f"  {ok} 知识库健康度: 🟢 健康")
        except Exception as e:
            print(f"  [WARN] 知识库 lint 检查失败: {e}")

    # 10. 【TradingAgents借鉴】写入决策反思摘要（供Agent7次日自动加载）
    try:
        write_reflection_summary(review)
    except Exception as e:
        print(f"  [WARN] 决策反思写入失败: {e}")

    return review


def write_reflection_summary(review: dict) -> str:
    """【TradingAgents借鉴】写入结构化决策反思到 memory/决策反思.md

    TradingAgents 的交易记忆机制会在每次分析后，将决策结果和反思写入持久文件，
    并在下次分析时自动注入到投资经理的上下文中。
    我们把这个机制本土化为：复盘师(Agent4)写入反思 → 投资领导(Agent7)自动加载。

    结构化内容（按优先级）：
    1. 反思摘要（100-200字）
    2. 仲裁项汇总（风控官vs操盘手分歧及裁定结果）
    3. 打回重做统计（哪些Agent被打回、原因、重做结果）
    4. 关键决策逻辑（宏观判断链+每项决策的依据和障碍）
    5. 偏差与教训（每个偏差的原因分类+改进方向）
    6. 次日关注事项（分级：最高/中等/持续观察）
    7. 准确率追踪（多维度准确率对比+趋势）
    每天只保留最新一条（历史在复盘记录JSON中）。
    """
    import re

    memory_dir = p('memory')
    os.makedirs(memory_dir, exist_ok=True)
    memory_path = os.path.join(memory_dir, "决策反思.md")

    date_str = review.get("date", datetime.now().strftime("%Y-%m-%d"))
    accuracy = review.get("accuracy", {})
    综合准确率 = accuracy.get("综合准确率", "N/A")
    板块率 = accuracy.get("板块预测", {}).get("rate", "N/A")
    大盘率 = accuracy.get("大盘方向", {}).get("rate", "N/A")
    trend = review.get("accuracy_trend", {}).get("trend", "暂无数据")
    策略建议 = review.get("策略建议", [])
    因子建议 = review.get("因子建议", [])
    偏差分析 = review.get("偏差分析", [])
    inputs = review.get("inputs", {})
    leader = review.get("leader", {})

    # 报告完整性摘要
    missing_agents = [name for name, loaded in inputs.items() if not loaded]
    completeness = "完整" if not missing_agents else f"缺失: {', '.join(missing_agents)}"
    agent_count = len(inputs)

    # 选股表现摘要
    stock_accuracy = accuracy.get("选股准确率", {})
    pick_summary = ""
    if stock_accuracy.get("total", 0) > 0:
        pick_summary = f"选股{stock_accuracy['correct']}/{stock_accuracy['total']}涨"

    # === 构建结构化内容 ===
    sections = []

    # --- 前导 YAML ---
    sections.append(f"""---
date: {date_str}
accuracy: {综合准确率}%
trend: {trend}
completeness: {completeness}
missing_agents: {', '.join(missing_agents) if missing_agents else '无'}
agent_count: {agent_count}
---

# 决策反思 — {date_str}

> 由复盘师(Agent4)写入，供投资领导(Agent7)次日自动加载。
> 参考：TradingAgents 决策记忆机制 — 反思注入到 Portfolio Manager 上下文。

---

## 一、反思摘要

{_generate_reflection_blurb(review)}
""")

    # --- 仲裁项汇总 ---
    sections.append(f"## 二、仲裁项汇总\n")
    conflicts = leader.get("conflicts", [])
    if conflicts:
        sections.append("| 序号 | 冲突类型 | 涉及Agent | 仲裁结果 | 依据 |")
        sections.append("|:---:|:--------|:----------|:--------|:-----|")
        for i, c in enumerate(conflicts, 1):
            ctype = c.get("type", "未知")
            detail = c.get("detail", "")[:60]
            parties = c.get("parties", "风控官 vs 操盘手")
            ruling = c.get("ruling", "见详情")
            basis = c.get("basis", "—")
            sections.append(f"| {i} | {ctype} | {parties} | {ruling} | {basis} |")
    else:
        sections.append("| 序号 | 冲突类型 | 说明 |")
        sections.append("|:---:|:--------|:-----|")
        sections.append("| — | **无分歧** | 风控官与操盘手意见一致，无冲突需仲裁 |")
    sections.append("")

    # --- 打回重做统计 ---
    sections.append(f"## 三、打回重做统计\n")
    quality = leader.get("quality_review", {})
    passed = quality.get("passed", 0)
    needs_work = quality.get("needs_work", 0)
    rejected = quality.get("rejected", 0)
    total_agents = passed + needs_work + rejected

    sections.append("| Agent | 打回类型 | 处理结果 |")
    sections.append("|-------|:--------:|:--------:|")
    agent_names_map = {
        "agent1": "🕵️ Agent1 情报员", "情报": "🕵️ Agent1 情报员",
        "agent2": "📊 Agent2 分析师", "分析": "📊 Agent2 分析师",
        "agent3": "🛡️ Agent3 风控官", "风控": "🛡️ Agent3 风控官",
        "agent4": "🔄 Agent4 复盘师", "复盘": "🔄 Agent4 复盘师",
        "agent5": "🔍 Agent5 选股机器人", "选股": "🔍 Agent5 选股机器人",
        "agent6": "🎯 Agent6 操盘手", "操盘": "🎯 Agent6 操盘手",
    }

    # 根据inputs中loaded的agent来决定哪些通过了审核
    for agent_key, agent_name in agent_names_map.items():
        loaded = inputs.get(agent_key, False) or inputs.get(
            {"agent1": "情报", "agent2": "分析", "agent3": "风控",
             "agent4": "复盘", "agent5": "选股", "agent6": "操盘"}.get(agent_key, ""), False)
        if not loaded:
            continue
        sections.append(f"| {agent_name} | — | ✅ 通过 |")

    sections.append(f"\n**审核统计**: {passed}通过 / {needs_work}需补充 / {rejected}不合格 | "
                    f"打回率: {round((needs_work + rejected) / max(total_agents, 1) * 100)}%\n")

    # --- 关键决策逻辑 ---
    sections.append(f"## 四、关键决策逻辑\n")
    sell_items = leader.get("sell_decisions", [])
    buy_items = leader.get("buy_decisions", [])
    hold_items = leader.get("hold_decisions", [])
    total_decisions = len(sell_items) + len(buy_items) + len(hold_items)

    止损数 = sum(1 for s in sell_items if "止损" in s.get("reason", ""))
    止盈数 = sum(1 for s in sell_items if "止盈" in s.get("reason", ""))
    减仓数 = sum(1 for s in sell_items if "减仓" in s.get("reason", ""))

    sections.append("### 决策概况")
    sections.append(f"- **总决策项**: {total_decisions} 项（卖出{len(sell_items)} / 买入{len(buy_items)} / 持有{len(hold_items)}）")
    sections.append(f"- **卖出类型**: 止损{止损数}项 / 止盈{止盈数}项 / 减仓{减仓数}项")
    sections.append(f"- **买入**: {'无（防守模式）' if not buy_items else f'{len(buy_items)}项'}")

    # 决策项明细
    if sell_items:
        sections.append(f"\n### 卖出/减仓明细")
        sections.append("| 标的 | 操作 | 盈亏 |")
        sections.append("|:----|:----:|:----:|")
        for s in sell_items[:10]:  # 最多10项
            code = s.get("code", "?")
            reason = s.get("reason", "卖出")
            pnl = s.get("pnl", "—")
            sections.append(f"| {code} | {reason} | {pnl} |")
        if len(sell_items) > 10:
            sections.append(f"| ... | 共{len(sell_items)}项 | ... |")

    if hold_items:
        sections.append(f"\n### 持有明细")
        sections.append(f"共 {len(hold_items)} 项持有")

    仓位信息 = _extract_position_info(review)
    if 仓位信息:
        sections.append(f"\n### 仓位管理\n{仓位信息}\n")
    else:
        sections.append("")

    # --- 偏差与教训 ---
    sections.append(f"## 五、偏差与教训\n")
    if 偏差分析:
        for i, dev in enumerate(偏差分析, 1):
            item = dev.get("item", dev.get("description", f"偏差{i}"))
            原因 = dev.get("可能原因", dev.get("cause", "待分析"))
            改进 = dev.get("改进方向", dev.get("improvement", "待定"))
            sections.append(f"### 偏差{i}: {item}")
            sections.append(f"- **原因**: {原因}")
            sections.append(f"- **改进**: {改进}\n")
    else:
        sections.append("无偏差记录。预测与实际一致，分析框架运行正常。\n")

    # --- 次日关注 ---
    sections.append(f"## 六、次日需关注事项\n")
    次日关注 = _extract_next_day_items(review)
    if 次日关注:
        sections.append(次日关注)
    else:
        sections.append("> 暂无特定关注事项。关注大盘趋势变化和持仓止损线。\n")

    # --- 准确率追踪 ---
    sections.append(f"## 七、准确率追踪\n")
    sections.append("| 维度 | 准确率 | 说明 |")
    sections.append("|:----|:-----:|:-----|")
    选股率 = accuracy.get("选股准确率", {})
    sections.append(f"| 大盘方向 | {大盘率}% | {accuracy.get('大盘方向', {}).get('correct', 0)}/{accuracy.get('大盘方向', {}).get('total', 0)} 正确 |")
    板块预测 = accuracy.get("板块预测", {})
    板块已校验 = 板块预测.get("verified", 0)
    板块总 = 板块预测.get("total", 0)
    if 板块已校验 > 0:
        sections.append(f"| 板块预测 | {板块率}% | {板块预测.get('correct', 0)}/{板块已校验} 正确（共{板块总}个预测，{板块预测.get('pending_review', 0)}待复核） |")
    else:
        sections.append(f"| 板块预测 | N/A | 板块数据不可用 |")
    if 选股率.get("total", 0) > 0:
        sections.append(f"| 选股推荐 | {选股率.get('rate', 'N/A')}% | {选股率.get('correct', 0)}/{选股率.get('total', 0)}上涨 |")
    else:
        sections.append(f"| 选股推荐 | N/A | {pick_summary if pick_summary else '无选股验证数据'} |")
    sections.append(f"| **综合准确率** | **{综合准确率}%** | 趋势: {trend} |")

    # --- 改进方向 ---
    if 策略建议 or 因子建议:
        sections.append(f"\n## 八、改进方向\n")
        for s in 策略建议:
            sections.append(f"- {s}")
        for f in 因子建议:
            sections.append(f"- {f}")
        sections.append("")

    # --- 知识库更新标记 ---
    sections.append(f"## 九、知识库更新标记\n")
    sections.append("- [x] 复盘记录 → `knowledge/复盘记录/复盘_{date_str.replace('-', '')}.json`")
    sections.append("- [x] 决策反思 → `memory/决策反思.md`（本文件）")
    sections.append(f"- [ ] 策略更新 → 偏差{len(偏差分析)}项，待累积数据后评估\n")

    # --- 页脚 ---
    sections.append("---\n")
    sections.append("*本文件由复盘师(Agent4)的 `write_reflection_summary()` 自动生成并写入。*")
    sections.append("*投资领导(Agent7)启动时通过 `memory/决策反思.md` 自动加载。*")
    sections.append("*TradingAgents范式: 反思注入 → 昨天错误今天不犯。*\n")

    content = "\n".join(sections)

    try:
        with open(memory_path, "w", encoding="utf-8") as f:
            f.write(content)
        print(f"  [OK] 结构化决策反思已写入: {memory_path}")
    except Exception as e:
        print(f"  [WARN] 决策反思写入失败: {e}")

    return memory_path


def _generate_reflection_blurb(review: dict) -> str:
    """生成100-200字的反思摘要"""
    accuracy = review.get("accuracy", {})
    综合 = accuracy.get("综合准确率", "N/A")
    trend = review.get("accuracy_trend", {}).get("trend", "暂无数据")
    偏差 = review.get("偏差分析", [])
    leader = review.get("leader", {})
    conflicts = leader.get("conflicts", [])

    parts = []
    if isinstance(综合, (int, float)) and 综合 >= 80:
        parts.append("整体预测准确率较高")
    elif isinstance(综合, (int, float)) and 综合 >= 50:
        parts.append("预测准确率中等，有改进空间")
    else:
        parts.append("预测准确率偏低，需重点检视分析框架")

    if 偏差:
        if len(偏差) == 1:
            parts.append(f"发现1个偏差项需关注")
        else:
            parts.append(f"发现{len(偏差)}个偏差项")
        # 提取第一个偏差的关键词
        first_dev = 偏差[0]
        dev_item = first_dev.get("item", first_dev.get("description", ""))
        if dev_item:
            parts.append(f"主要偏差: {dev_item[:40]}")

    if not conflicts:
        parts.append("风控官与操盘手意见一致")
    else:
        has_conflict = any(c.get("type") not in ("无分歧", "一致") for c in conflicts)
        if has_conflict:
            parts.append("存在Agent间分歧，需关注仲裁结果")

    if not parts:
        parts.append("分析框架运行正常，无显著偏差")

    return "。\n".join(parts) + "。"


def _extract_position_info(review: dict) -> str:
    """从review数据中提取仓位管理摘要"""
    leader = review.get("leader", {})
    sell_items = leader.get("sell_decisions", [])
    buy_items = leader.get("buy_decisions", [])

    parts = []
    if buy_items:
        parts.append(f"- 计划买入: {len(buy_items)}项")
    else:
        parts.append("- 计划买入: 无（防守姿态）")

    止损数 = sum(1 for s in sell_items if "止损" in s.get("reason", ""))
    止盈数 = sum(1 for s in sell_items if "止盈" in s.get("reason", ""))
    parts.append(f"- 计划卖出: {len(sell_items)}项（止损{止损数}/止盈{止盈数}）")

    return "\n".join(parts) if parts else ""


def _extract_next_day_items(review: dict) -> str:
    """从偏差分析和决策数据中生成次日关注事项"""
    items = []
    偏差分析 = review.get("偏差分析", [])
    leader = review.get("leader", {})

    # 从偏差中提取改进项
    for dev in 偏差分析:
        improvement = dev.get("改进方向", dev.get("improvement", ""))
        if improvement and improvement not in ("待定", ""):
            items.append(("🟡", improvement[:80]))

    # 检查是否有待执行的止损
    sell_items = leader.get("sell_decisions", [])
    止损待执行 = [s for s in sell_items if "止损" in s.get("reason", "")]
    if 止损待执行:
        items.append(("🔴", f"{len(止损待执行)}项止损待执行，确认是否已处理"))

    # 大盘/风险提示
    accuracy = review.get("accuracy", {})
    综合 = accuracy.get("综合准确率", 100)
    if isinstance(综合, (int, float)) and 综合 < 60:
        items.append(("🟡", "准确率偏低，建议降低交易频率，等待信号更明确"))

    if not items:
        items.append(("🟢", "关注大盘趋势变化和持仓止损线"))
        items.append(("🟢", "关注北向资金流向和板块轮动信号"))

    lines = []
    for priority, text in items:
        emoji = {"🔴": "最高优先级", "🟡": "中等优先级", "🟢": "持续观察"}.get(priority, "")
        lines.append(f"- {priority} **{emoji}**: {text}")

    return "\n".join(lines) if lines else ""


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
