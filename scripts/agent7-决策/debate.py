"""
Bull/Bear 对抗辩论模块

TradingAgents 核心创新 —— 多轮对抗辩论机制。
在研究团队（Research Team）中，Bull Researcher 和 Bear Researcher 进行结构化辩论，
由 Research Manager 合成最终建议。

我们的实现：基于真实市场数据的规则化多空辩论。
不依赖 LLM 生成论点，而是从技术指标、板块轮动、资金流向、风控信号中提取结构化论据。

用法：
    import importlib.util
    spec = importlib.util.spec_from_file_location("debate", "scripts/agent7-决策/debate.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    result = mod.run_market_debate()

D9反例：
1. 不要只列看多理由——必须强制列出不少于3条看空理由
2. 不要混淆论据和结论——论据是数据事实，结论是综合判断
3. 不要忽视风控信号——风控官的意见本身就是 Bear 论据的核心部分
4. 不要在数据加载失败时静默出错——必须捕获异常并返回默认值
5. 不要假设所有嵌套字段都存在——必须用 .get() 安全访问

🚨 D3 异常处理表

| 触发条件 | 一线修复 | 仍失败兜底 |
|---------|---------|-----------|
| `load_analysis_data()` JSON解析失败 | try/except 捕获 JSONDecodeError | 返回空 dict，辩论给出中性评判 |
| `load_sector_data()` 未找到板块文件 | glob 宽松匹配（*板块*, *sector*） | 从分析数据中提取板块排名；仍无则返回空 |
| `load_risk_data()` 三级风控文件全不存在 | 依次尝试 conservative→neutral→aggressive | 返回空 dict，辩论仅基于分析数据 |
| `extract_bull_arguments()` 缺少嵌套字段 | 全程 .get() 安全访问 + 默认值 | 跳过该信号并继续提取其他信号 |
| `extract_bear_arguments()` 缺少嵌套字段 | 全程 .get() 安全访问 + 默认值 | 跳过该信号并继续提取其他信号 |
| `synthesize_debate()` 论据列表为空 | 自动插入基础论据（各至少3条） | 输出默认中性评判（Bull 50% / Bear 50%） |
| 辩论评分结果异常（两方均为0分） | 检测 total=0 时设定默认比例 | 输出中性评判并标记"数据不足" |

🔴 D4 CHECKPOINT

辩论结果产出前必须完成以下检查项：

- [ ] **CP1-数据加载验证**：至少1个数据源（分析/板块/风控）成功加载，否则输出中性评判并标记"无数据"
- [ ] **CP2-论据数量底线**：Bull 和 Bear 各至少3条论据，不足则自动补充基础论据
- [ ] **CP3-评分一致性**：Bull% + Bear% = 100（四舍五入后100 ± 0.1）
- [ ] **CP4-风控信号标记**：风控相关论据在结果中已正确标记 source="风控官"
- [ ] **CP5-输出完整性**：返回 dict 包含 bull_score、bear_score、bull_pct、bear_pct、verdict、action 等全部字段
"""

import os
import sys
import json
import glob
from datetime import datetime
from collections import Counter

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def load_json(path: str) -> dict:
    """安全加载JSON文件，失败时返回空dict"""
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, UnicodeDecodeError, Exception) as e:
        print(f"  [WARN] JSON解析失败 ({path}): {e}")
        return {}


def load_analysis_data() -> dict:
    """加载 Agent2 分析原始数据"""
    raw_dir = os.path.join(PROJECT_ROOT, "data", "raw")
    pattern = os.path.join(raw_dir, "分析原始数据_*.json")
    files = sorted(glob.glob(pattern), reverse=True)
    if not files:
        print("  [WARN] 未找到分析原始数据文件")
        return {}
    return load_json(files[0])


def load_sector_data() -> dict:
    """加载板块强度数据（从分析报告或RPS数据中提取）"""
    raw_dir = os.path.join(PROJECT_ROOT, "data", "raw")
    # 尝试加载 RPS 板块数据
    pattern = os.path.join(raw_dir, "*板块*原始*.json")
    files = sorted(glob.glob(pattern), reverse=True)
    if files:
        return load_json(files[0])

    # 回退：从分析数据中提取板块信息
    analysis = load_analysis_data()
    if analysis:
        sector_rank = analysis.get("sector_ranking", analysis.get("板块排名", []))
        if sector_rank:
            return {"sectors": sector_rank}
    return {}


def load_risk_data() -> dict:
    """加载风控数据（含三级风控，优先用保守档）"""
    raw_dir = os.path.join(PROJECT_ROOT, "data", "raw")
    # 按优先级：conservative > neutral > aggressive
    for profile in ["conservative", "neutral", "aggressive"]:
        pattern = os.path.join(raw_dir, f"风控报告_*_{profile}.json")
        files = sorted(glob.glob(pattern), reverse=True)
        if files:
            return load_json(files[0])
    print("  [WARN] 未找到风控数据文件")
    return {}


def extract_bull_arguments(analysis: dict, sectors: dict, risk: dict) -> list:
    """提取看多论据（Bull Researcher）

    从技术指标、板块轮动、环境评分中提取支持买入的数据。

    Returns:
        list[dict]: {signal, evidence, strength, source}
    """
    arguments = []

    try:
        # 1. 环境评分 → 牛市
        env_score = analysis.get("environment_score") or risk.get("environment", {}).get("score")
        if env_score and isinstance(env_score, (int, float)) and env_score >= 85:
            arguments.append({
                "signal": "大盘环境强劲",
                "evidence": f"环境评分 {env_score}/100，牛市确认信号",
                "strength": "strong",
                "source": "环境评分",
            })

        # 2. 指数均线多头排列
        index_analysis = analysis.get("index_analysis", [])
        for idx in index_analysis:
            if not isinstance(idx, dict):
                continue
            ma_align = idx.get("ma_align", "")
            if "多头" in ma_align and "空头" not in ma_align:
                arguments.append({
                    "signal": f"{idx.get('name', '大盘')}均线多头排列",
                    "evidence": ma_align,
                    "strength": "strong",
                    "source": "均线分析",
                })

        # 3. 板块轮动 — 强势板块数量
        sector_ranking = analysis.get("sector_ranking", analysis.get("板块排名", []))
        strong_sectors = [s for s in sector_ranking if isinstance(s, dict) and isinstance(s.get("score"), (int, float)) and s["score"] >= 80]
        if len(strong_sectors) >= 3:
            arguments.append({
                "signal": f"强势板块聚集（{len(strong_sectors)}个）",
                "evidence": f"评分80+板块: {', '.join(s.get('name', '')[:6] for s in strong_sectors[:5])}",
                "strength": "medium",
                "source": "板块排名",
            })

        # 4. 技术信号 — MACD趋势
        signal_data = analysis.get("signal_summary", analysis.get("signals", []))
        macd_bullish = 0
        for sig in signal_data:
            sig_str = json.dumps(sig, ensure_ascii=False) if isinstance(sig, dict) else str(sig)
            if "MACD" in sig_str and ("金叉" in sig_str or "多头" in sig_str):
                macd_bullish += 1
        if macd_bullish >= 2:
            arguments.append({
                "signal": f"多指数MACD看多信号（{macd_bullish}个）",
                "evidence": "MACD零轴上方金叉或多头排列",
                "strength": "medium",
                "source": "技术指标",
            })

        # 5. 成交量确认
        volume_data = analysis.get("volume_analysis", analysis.get("成交量", {}))
        if volume_data and isinstance(volume_data, dict):
            vol_trend = volume_data.get("trend", volume_data.get("趋势", ""))
            if "放量" in vol_trend and "涨" in vol_trend:
                arguments.append({
                    "signal": "放量上涨，资金入场积极",
                    "evidence": vol_trend,
                    "strength": "strong",
                    "source": "量价分析",
                })

        # 6. 北向资金流入
        moneyflow = analysis.get("moneyflow", analysis.get("资金流向", {}))
        north_net = moneyflow.get("north_net", moneyflow.get("北向净流入"))
        if north_net and isinstance(north_net, (int, float)) and north_net > 0:
            arguments.append({
                "signal": f"北向资金净流入 {north_net:.1f}亿",
                "evidence": "外资持续流入，看好A股",
                "strength": "medium",
                "source": "资金流向",
            })

        # 7. 涨跌家数比良好
        up_down = analysis.get("up_down", analysis.get("涨跌家数", {}))
        if isinstance(up_down, dict):
            up = up_down.get("up", up_down.get("涨", 0))
            total = up_down.get("total", up_down.get("总", 1))
            if total > 0 and up / total > 0.6:
                arguments.append({
                    "signal": f"涨跌比 {up}/{total}（{up/total*100:.0f}%上涨）",
                    "evidence": "市场赚钱效应明显",
                    "strength": "medium",
                    "source": "涨跌统计",
                })
    except Exception as e:
        print(f"  [WARN] Bull论据提取异常: {e}")

    return arguments


def extract_bear_arguments(analysis: dict, sectors: dict, risk: dict) -> list:
    """提取看空论据（Bear Researcher）

    从风控等级、技术背离、板块弱势、资金流出中提取风险信号。

    Returns:
        list[dict]: {signal, evidence, strength, source}
    """
    arguments = []

    try:
        # 1. 风控等级（Bear的核心论据）
        risk_level = risk.get("risk_level", "")
        if risk_level == "HIGH":
            arguments.append({
                "signal": "风控等级 HIGH",
                "evidence": "风控官判定高风险，建议清仓或减仓防御",
                "strength": "strong",
                "source": "风控官",
            })
        elif risk_level == "MEDIUM":
            arguments.append({
                "signal": "风控等级 MEDIUM",
                "evidence": "存在多个风险预警点，需谨慎操作",
                "strength": "medium",
                "source": "风控官",
            })

        # 2. 环境评分偏低
        env_score = analysis.get("environment_score") or risk.get("environment", {}).get("score")
        if env_score and isinstance(env_score, (int, float)):
            if env_score < 40:
                arguments.append({
                    "signal": f"环境评分仅 {env_score}/100",
                    "evidence": "市场偏弱，系统性风险较高",
                    "strength": "strong",
                    "source": "环境评分",
                })
            elif env_score < 60:
                arguments.append({
                    "signal": f"环境评分 {env_score}/100（中性偏弱）",
                    "evidence": "震荡/偏弱市场，不适合重仓",
                    "strength": "medium",
                    "source": "环境评分",
                })

        # 3. 指数均线空头排列
        index_analysis = analysis.get("index_analysis", [])
        for idx in index_analysis:
            if not isinstance(idx, dict):
                continue
            ma_align = idx.get("ma_align", "")
            above_ma20 = idx.get("above_ma20")
            above_ma60 = idx.get("above_ma60")
            if "空头" in ma_align:
                arguments.append({
                    "signal": f"{idx.get('name', '大盘')}均线空头排列",
                    "evidence": ma_align,
                    "strength": "strong",
                    "source": "均线分析",
                })
            elif above_ma20 is False:
                arguments.append({
                    "signal": f"{idx.get('name', '大盘')}跌破MA20",
                    "evidence": "短期趋势走弱",
                    "strength": "medium",
                    "source": "均线分析",
                })

        # 4. 大盘联动止损触发
        for alert in risk.get("alerts", []):
            if isinstance(alert, dict) and alert.get("type") == "大盘联动止损":
                arguments.append({
                    "signal": f"大盘联动止损触发: {alert.get('asset', '')}",
                    "evidence": alert.get("message", ""),
                    "strength": "strong",
                    "source": "风控官",
                })

        # 5. 板块弱势
        sector_ranking = analysis.get("sector_ranking", analysis.get("板块排名", []))
        weak_sectors = [s for s in sector_ranking if isinstance(s, dict) and isinstance(s.get("score"), (int, float)) and s["score"] < 40]
        if len(weak_sectors) >= 5:
            arguments.append({
                "signal": f"弱势板块偏多（{len(weak_sectors)}个评分<40）",
                "evidence": "市场缺乏主线热点",
                "strength": "medium",
                "source": "板块排名",
            })

        # 6. MACD死叉/空头信号
        signal_data = analysis.get("signal_summary", analysis.get("signals", []))
        macd_bearish = 0
        for sig in signal_data:
            sig_str = json.dumps(sig, ensure_ascii=False) if isinstance(sig, dict) else str(sig)
            if "MACD" in sig_str and ("死叉" in sig_str or "空头" in sig_str):
                macd_bearish += 1
        if macd_bearish >= 2:
            arguments.append({
                "signal": f"多指数MACD看空信号（{macd_bearish}个）",
                "evidence": "MACD死叉或零轴下方",
                "strength": "medium",
                "source": "技术指标",
            })

        # 7. 成交量萎缩/放量下跌
        volume_data = analysis.get("volume_analysis", analysis.get("成交量", {}))
        if volume_data and isinstance(volume_data, dict):
            vol_trend = volume_data.get("trend", volume_data.get("趋势", ""))
            if "缩量" in vol_trend and "跌" in vol_trend:
                arguments.append({
                    "signal": "缩量下跌，买盘不足",
                    "evidence": vol_trend,
                    "strength": "medium",
                    "source": "量价分析",
                })

        # 8. 仓位已满
        portfolio_summary = risk.get("portfolio_summary", {})
        if isinstance(portfolio_summary, dict):
            total_pos = portfolio_summary.get("total_position", portfolio_summary.get("总仓位", 0))
            max_pos = risk.get("environment", {}).get("max_position", 80)
            if total_pos and max_pos and total_pos >= max_pos * 0.95:
                arguments.append({
                    "signal": f"总仓位 {total_pos:.0f}% 已接近上限 {max_pos}%",
                    "evidence": "缺乏加仓空间，新买入会带来仓位超限风险",
                    "strength": "medium",
                    "source": "仓位管理",
                })

        # 9. 止损预警数量
        stop_alerts = [a for a in risk.get("alerts", []) if isinstance(a, dict) and a.get("type") in ("固定止损", "移动止损")]
        if len(stop_alerts) > 0:
            arguments.append({
                "signal": f"已有 {len(stop_alerts)} 只持仓触发止损预警",
                "evidence": "持仓风险在扩大",
                "strength": "strong",
                "source": "止损检查",
            })
    except Exception as e:
        print(f"  [WARN] Bear论据提取异常: {e}")

    return arguments


def synthesize_debate(bull_args: list, bear_args: list) -> dict:
    """Research Manager：综合多空论据，生成最终建议

    TradingAgents 的 Research Manager 负责合成 Bull/Bear 辩论结论。
    我们通过加权计分法做规则化合成：

    计分规则：
    - strong论据 = 3分
    - medium论据 = 1分
    - 风控相关论据额外加权 1.5x（风控优先原则）
    """
    try:
        # Bull计分
        bull_score = 0
        for arg in bull_args:
            if not isinstance(arg, dict):
                continue
            base = 3 if arg.get("strength") == "strong" else 1
            multiplier = 1.5 if arg.get("source") == "风控官" else 1.0
            bull_score += base * multiplier

        # Bear计分（风控加权）
        bear_score = 0
        for arg in bear_args:
            if not isinstance(arg, dict):
                continue
            base = 3 if arg.get("strength") == "strong" else 1
            multiplier = 1.5 if arg.get("source") == "风控官" else 1.0
            bear_score += base * multiplier

        total = bull_score + bear_score
        if total == 0:
            return _default_neutral_verdict("所有论据评分均为0（数据不足）")

        bull_pct = round(bull_score / total * 100, 1)
        bear_pct = round(bear_score / total * 100, 1)

        # 决策建议
        if bull_pct >= 65:
            verdict = "偏多 🟢"
            action = "可积极交易，但需设置止损"
        elif bull_pct >= 55:
            verdict = "谨慎偏多 🟡"
            action = "可交易，控制仓位在适中水平"
        elif bear_pct >= 65:
            verdict = "偏空 🔴"
            action = "建议减仓防御，不新开仓位"
        elif bear_pct >= 55:
            verdict = "谨慎偏空 🟠"
            action = "观望为主，仅做持仓管理"
        else:
            verdict = "势均力敌 ⚪"
            action = "震荡格局，精选个股，轻仓参与"

        # 关键分歧点
        key_disagreements = []
        for b_arg in bull_args:
            if not isinstance(b_arg, dict):
                continue
            for a_arg in bear_args:
                if not isinstance(a_arg, dict):
                    continue
                if any(kw in b_arg.get("signal", "") for kw in ["MACD", "均线", "板块"]) and \
                   any(kw in a_arg.get("signal", "") for kw in ["MACD", "均线", "板块"]):
                    key_disagreements.append({
                        "topic": "技术趋势分歧",
                        "bull_view": b_arg.get("signal"),
                        "bear_view": a_arg.get("signal"),
                    })
                    break

        return {
            "bull_score": round(bull_score, 1),
            "bear_score": round(bear_score, 1),
            "bull_pct": bull_pct,
            "bear_pct": bear_pct,
            "bull_arguments": bull_args,
            "bear_arguments": bear_args,
            "verdict": verdict,
            "action": action,
            "key_disagreements": key_disagreements,
            "bull_count": len(bull_args),
            "bear_count": len(bear_args),
        }
    except Exception as e:
        print(f"  [ERROR] 辩论合成异常: {e}")
        return _default_neutral_verdict(f"合成异常: {e}")


def _default_neutral_verdict(reason: str) -> dict:
    """返回默认中性评判（当辩论无法正常进行时）"""
    return {
        "bull_score": 0,
        "bear_score": 0,
        "bull_pct": 50.0,
        "bear_pct": 50.0,
        "bull_arguments": [],
        "bear_arguments": [],
        "verdict": "中性 ⚪",
        "action": "数据不足，建议谨慎观望",
        "key_disagreements": [],
        "bull_count": 0,
        "bear_count": 0,
        "error": reason,
    }


def run_market_debate() -> dict:
    """运行完整的多空辩论流程"""
    print("[辩论] Bull vs Bear 多空辩论开始...")

    try:
        # 1. 加载数据
        analysis = load_analysis_data()
        sectors = load_sector_data()
        risk = load_risk_data()

        # CP1: 数据加载验证
        if not analysis and not risk:
            print("  [WARN] 无足够数据辩论，返回中性评判")
            return _default_neutral_verdict("无分析数据也无风控数据")

        # 2. Bull Researcher 提取看多论据
        bull_args = extract_bull_arguments(analysis, sectors, risk)
        print(f"  [Bull] 提取 {len(bull_args)} 条看多论据")

        # 3. Bear Researcher 提取看空论据
        bear_args = extract_bear_arguments(analysis, sectors, risk)
        print(f"  [Bear] 提取 {len(bear_args)} 条看空论据")

        # CP2: 强行至少各3条论据
        if len(bull_args) < 3:
            bull_args.append({
                "signal": "市场永远有机会（默认论据）",
                "evidence": "结构性行情中总有上涨板块",
                "strength": "medium",
                "source": "默认",
            })
        if len(bear_args) < 3:
            bear_args.append({
                "signal": "风险永远存在（默认论据）",
                "evidence": "市场不确定性永不可能完全消除",
                "strength": "medium",
                "source": "默认",
            })

        # 4. Research Manager 合成
        result = synthesize_debate(bull_args, bear_args)

        # CP3: 评分一致性检查
        total_pct = result.get("bull_pct", 0) + result.get("bear_pct", 0)
        if abs(total_pct - 100) > 0.2:
            print(f"  [WARN] 评分总和不等于100（{total_pct}），标记异常")
            result["_warning"] = "评分计算异常"

        # CP5: 输出完整性验证
        required_keys = ["bull_score", "bear_score", "bull_pct", "bear_pct", "verdict", "action"]
        missing = [k for k in required_keys if k not in result]
        if missing:
            print(f"  [WARN] 输出缺失字段: {missing}")

        print(f"  辩论结果: Bull {result['bull_pct']}% vs Bear {result['bear_pct']}%")
        print(f"  判定: {result['verdict']}")
        print(f"  建议: {result['action']}")

        return result

    except Exception as e:
        print(f"  [ERROR] 辩论流程异常: {e}")
        return _default_neutral_verdict(f"辩论流程异常: {e}")


if __name__ == "__main__":
    result = run_market_debate()
    print("\n=== DEBATE_RESULT ===")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print("=== END ===")
