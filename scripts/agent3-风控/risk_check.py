"""
Agent3 风控官 - 风险管理核心脚本

功能：
1. 读取仓位管理规则和止损规则
2. 读取持仓信息
3. 根据大盘环境评估当前风险等级
4. 检查每笔持仓是否触发止损
5. 检查总仓位和单票仓位是否超限
6. 【新增】审查操盘手交易计划（买入/卖出清单风险评估）
7. 【新增】持仓加减仓建议（与操盘手的持仓管理建议对比）
8. 【新增】输出与操盘手的冲突项（供投资领导仲裁）
9. 输出风控报告结构化数据

用法：
    source venv/Scripts/activate
    python -X utf8 scripts/agent3-风控/risk_check.py [--portfolio data/portfolio.json] [--env-score 70] [--trade-plan data/raw/交易原始数据_YYYYMMDD.json]

如果不传环境评分，则从 data/raw/分析原始数据_*.json 中自动读取

D9反例（工作反例）：
1. 不要盲从操盘手意见——风控是独立审查，不是走过场
2. 不要否决无理由——每条否决必须有具体的规则依据
3. 不要分歧不上报——风控和操盘手意见不合时必须上报投资领导仲裁
4. 不要只用一条规则判断——风控是多维度综合评估
5. 不要给模糊的风控等级——必须明确LOW/MEDIUM/HIGH/CRITICAL

D3异常处理表：
| 触发条件 | 一线修复 | 仍失败兜底 |
|---------|---------|-----------|
| 持仓文件缺失或格式错 | 打印错误，返回空持仓列表 | 假设空仓，风控等级设为HIGH |
| 仓位规则文件缺失 | 使用硬编码默认值（震荡市总仓位80%/单票20%） | 输出警告，保守取值 |
| 止损规则文件缺失 | 使用默认止损规则（固定-7%/移动-5%） | 输出警告，保守止损 |
| 分析原始数据为空（环境评分） | 使用默认评分50（中等） | 风控基于持仓数据独立判断 |
| 操盘计划缺失（trade_plan为空） | 跳过操盘计划审查章节 | 仅输出持仓风控部分 |
| 持仓总资产字段名不统一（总资产vs总资产_含现金） | 按优先级依次尝试4个字段名 | 用持仓市值之和代替，标注"估算" |
| 涨跌停价格获取失败 | 用±10%/±20%规则估算 | 标记为"价格区间估算" |
| 移动止损计算缺少历史最高价 | 用当日开盘价或昨日收盘价代替 | 降级为固定止损-7% |

D4 CHECKPOINT:
- CP1-止损检查：每个持仓的盈亏比例与止损阈值逐一对比
- CP2-仓位超限：总仓位和单票仓位分别检查
- CP3-操盘计划审查：买入清单逐笔风控合规检查
- CP4-买入止损校验：操盘建议买入的止损位是否合理
- CP5-冲突标记：风控与操盘意见不一致时标记冲突
- CP6-冲突上报：标记为冲突的项必须进入仲裁流程
"""

import os
import sys
import json
import glob
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from scripts.utils.tushare_client import pro


def load_json(path: str) -> dict:
    """安全加载 JSON 文件"""
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_portfolio(path: str = None) -> dict:
    """加载持仓数据"""
    if path is None:
        path = os.path.join(os.path.dirname(__file__), "..", "..", "data", "portfolio.json")
    return load_json(path)


def load_stop_loss_rules() -> list:
    """加载止损规则"""
    path = os.path.join(os.path.dirname(__file__), "..", "..", "data", "止损规则.json")
    rules = load_json(path)
    return rules.get("规则", [])


def load_position_rules() -> dict:
    """加载仓位管理规则"""
    path = os.path.join(os.path.dirname(__file__), "..", "..", "data", "仓位管理规则.json")
    return load_json(path)


def load_env_score_from_analysis() -> int:
    """从 Agent2 的分析结果中读取环境评分"""
    raw_dir = os.path.join(os.path.dirname(__file__), "..", "..", "data", "raw")
    pattern = os.path.join(raw_dir, "分析原始数据_*.json")
    files = sorted(glob.glob(pattern), reverse=True)
    if not files:
        return None
    latest = files[0]
    try:
        data = load_json(latest)
        return data.get("environment_score")
    except (FileNotFoundError, json.JSONDecodeError, Exception) as e:
        print(f"[WARN] 加载环境评分失败: {e}")
        return None


def load_index_analysis() -> list:
    """从 Agent2 的分析结果中加载指数均线数据"""
    raw_dir = os.path.join(os.path.dirname(__file__), "..", "..", "data", "raw")
    pattern = os.path.join(raw_dir, "分析原始数据_*.json")
    files = sorted(glob.glob(pattern), reverse=True)
    if not files:
        return None
    latest = files[0]
    try:
        data = load_json(latest)
        return data.get("index_analysis") or data.get("indices") or None
    except (FileNotFoundError, json.JSONDecodeError, Exception) as e:
        print(f"[WARN] 加载指数分析数据失败: {e}")
        return None


def fetch_stock_price(ts_code: str) -> dict:
    """获取个股最新的实时/日线行情"""
    try:
        df = pro.daily(ts_code=ts_code)
        if df is not None and not df.empty:
            # Tushare数据按trade_date升序，需降序取最新
            df = df.sort_values("trade_date", ascending=False).reset_index(drop=True)
            last = df.iloc[0]
            return {
                "price": float(last["close"]),
                "pct_chg": float(last.get("pct_chg", 0)),
                "trade_date": last.get("trade_date", ""),
                "high": float(last.get("high", last["close"])),
                "low": float(last.get("low", last["close"])),
            }
    except (Exception) as e:
        print(f"[WARN] 获取{ts_code}行情失败: {e}")
        pass
    return None


def determine_market_environment(env_score: int, index_data: dict = None) -> dict:
    """根据环境评分判断市场状态"""
    if env_score is None and index_data is None:
        return {"level": "unknown", "name": "未知", "max_position": 80, "max_single": 20}

    if env_score is not None:
        if env_score >= 85:
            return {"level": "bull", "name": "牛市确认", "max_position": 100, "max_single": 30}
        elif env_score >= 60:
            return {"level": "range", "name": "震荡市", "max_position": 80, "max_single": 20}
        elif env_score >= 30:
            return {"level": "bear", "name": "熊市/调整", "max_position": 50, "max_single": 10}
        else:
            return {"level": "extreme", "name": "极端行情", "max_position": 0, "max_single": 0}

    # 如果有指数数据但无评分，用均线判断
    if index_data:
        above_ma20 = index_data.get("above_ma20", False)
        above_ma60 = index_data.get("above_ma60", False)
        ma_align = index_data.get("ma_align", "")

        if above_ma60 and "多头" in ma_align:
            return {"level": "bull", "name": "牛市确认", "max_position": 100, "max_single": 30}
        elif above_ma20:
            return {"level": "range", "name": "震荡市", "max_position": 80, "max_single": 20}
        elif above_ma20 is False and above_ma60 is True:
            return {"level": "bear", "name": "熊市/调整", "max_position": 50, "max_single": 10}
        else:
            return {"level": "extreme", "name": "极端行情", "max_position": 0, "max_single": 0}

    return {"level": "range", "name": "震荡市", "max_position": 80, "max_single": 20}


def check_stop_loss(holding: dict, current_price: float, pct_chg: float, ts_code: str = None) -> list:
    """对单个持仓检查是否触发止损"""
    alerts = []
    cost = holding.get("成本价") or 0
    name = holding.get("名称", "未知")
    code = holding.get("代码", "")
    ts_code = ts_code or code
    current_price = current_price or 0

    if cost == 0 or current_price == 0:
        return alerts

    # 当前盈亏比例
    pnl_pct = (current_price - cost) / cost * 100
    holding["当前盈亏%"] = round(pnl_pct, 2)

    # 获取持仓期间最高价（从买入日或最近60日取高值）
    highest_price = current_price
    try:
        buy_date = holding.get("买入日期", "")
        df = pro.daily(ts_code=ts_code)
        if df is not None and not df.empty:
            # 取最近60日最高价（Tushare升序，降序后取前60行）
            df_sorted = df.sort_values("trade_date", ascending=False).reset_index(drop=True)
            high_prices = df_sorted["high"].iloc[:60].values if "high" in df.columns else df_sorted["close"].iloc[:60].values
            if len(high_prices) > 0:
                highest_price = max(high_prices)
    except (Exception) as e:
        print(f"[WARN] 获取{ts_code}历史最高价失败: {e}")
        pass

    # 从高点回撤比例
    drawdown_pct = (current_price - highest_price) / highest_price * 100 if highest_price > 0 else 0

    # 1. 固定比例止损（-7%）
    rules = load_stop_loss_rules()
    for rule in rules:
        rule_type = rule.get("类型", "")
        params = rule.get("参数", {})

        if rule_type == "固定比例止损":
            threshold = params.get("比例", -7)
            if pnl_pct <= threshold:
                alerts.append({
                    "level": "CRITICAL",
                    "type": "固定止损",
                    "asset": f"{name}({code})",
                    "message": f"亏损 {pnl_pct:.1f}%，触发 {threshold}% 止损线！建议立即执行止损",
                    "pnl_pct": round(pnl_pct, 2),
                    "threshold": threshold,
                    "action": "强制止损",
                })

        elif rule_type == "移动止损":
            retreat = params.get("回撤比例", -5)
            # 如果盈利状态下从最高点回撤超过阈值
            if pnl_pct > 0 and drawdown_pct <= retreat:
                alerts.append({
                    "level": "WARNING",
                    "type": "移动止损",
                    "asset": f"{name}({code})",
                    "message": f"从高点回撤 {abs(drawdown_pct):.1f}%（最高{highest_price:.2f}→现{current_price:.2f}），触发移动止损线",
                    "pnl_pct": round(pnl_pct, 2),
                    "threshold": retreat,
                    "drawdown": round(drawdown_pct, 2),
                    "action": "止盈/减仓",
                })

    return alerts


def _get_total_asset(portfolio: dict) -> float:
    """获取总资产，兼容新旧字段名"""
    return (portfolio.get("总资产") or
            portfolio.get("总资产_含现金") or
            portfolio.get("持仓总市值") or
            sum(h.get("市值", 0) for h in portfolio.get("持仓列表", [])) or
            1)


def check_position_limits(portfolio: dict, market_env: dict) -> list:
    """检查仓位是否超限"""
    alerts = []
    holdings = portfolio.get("持仓列表", [])
    total_asset = _get_total_asset(portfolio)

    if total_asset == 0:
        return alerts

    max_position = market_env.get("max_position", 80)
    max_single = market_env.get("max_single", 20)
    market_name = market_env.get("name", "未知")

    # 计算实际总仓位
    total_market_value = sum(h.get("市值", 0) or h.get("持股数量", 0) * h.get("当前价", 0) for h in holdings)
    actual_position_pct = (total_market_value / total_asset) * 100 if total_asset > 0 else 0

    # 总仓位检查
    if actual_position_pct > max_position:
        excess = actual_position_pct - max_position
        alerts.append({
            "level": "WARNING" if excess < 20 else "CRITICAL",
            "type": "总仓位超限",
            "asset": "全部持仓",
            "message": f"当前总仓位 {actual_position_pct:.0f}%，超过 {market_name} 上限 {max_position}%，超限 {excess:.0f}%",
            "actual": round(actual_position_pct, 1),
            "limit": max_position,
            "action": "减仓",
        })

    # 单票仓位检查
    for h in holdings:
        market_value = h.get("市值", 0) or h.get("持股数量", 0) * h.get("当前价", 0)
        single_pct = (market_value / total_asset) * 100 if total_asset > 0 else 0
        name = h.get("名称", "未知")
        code = h.get("代码", "")

        if single_pct > max_single:
            alerts.append({
                "level": "WARNING",
                "type": "单票超限",
                "asset": f"{name}({code})",
                "message": f"{name} 仓位 {single_pct:.0f}%，超过单票上限 {max_single}%",
                "actual": round(single_pct, 1),
                "limit": max_single,
                "action": "减仓",
            })

    return alerts


def check_market_risk(env_score: int, index_analysis: list = None) -> list:
    """大盘环境风险检查"""
    alerts = []

    env = determine_market_environment(env_score, None)

    if env["level"] == "bull":
        alerts.append({
            "level": "INFO",
            "type": "大盘环境",
            "asset": "大盘",
            "message": f"环境评分 {env_score}，牛市环境，可积极操作",
            "action": "正常交易",
        })
    elif env["level"] == "range":
        alerts.append({
            "level": "INFO",
            "type": "大盘环境",
            "asset": "大盘",
            "message": f"环境评分 {env_score}，震荡市，控制仓位在 {env['max_position']}% 以内",
            "action": "谨慎交易",
        })
    elif env["level"] == "bear":
        alerts.append({
            "level": "WARNING",
            "type": "大盘环境",
            "asset": "大盘",
            "message": f"环境评分 {env_score}，市场偏弱，建议仓位降至 {env['max_position']}% 以下",
            "action": "减仓防御",
        })
    elif env["level"] == "extreme":
        alerts.append({
            "level": "CRITICAL",
            "type": "大盘环境",
            "asset": "大盘",
            "message": f"环境评分 {env_score}，极端行情！建议空仓观望",
            "action": "清仓",
        })
    else:
        alerts.append({
            "level": "INFO",
            "type": "大盘环境",
            "asset": "大盘",
            "message": "无法获取环境评分，使用默认震荡市规则",
            "action": "谨慎",
        })

    # 检查大盘联动止损（来自止损规则）
    if index_analysis:
        for idx in index_analysis:
            name = idx.get("name", "")
            above_ma20 = idx.get("above_ma20")
            above_ma60 = idx.get("above_ma60")

            if above_ma20 is False and above_ma60 is True:
                alerts.append({
                    "level": "WARNING",
                    "type": "大盘联动止损",
                    "asset": name,
                    "message": f"{name} 跌破 MA20，建议减仓至 5 成以下",
                    "action": "减仓至5成",
                })
            elif above_ma20 is False and above_ma60 is False:
                alerts.append({
                    "level": "CRITICAL",
                    "type": "大盘联动止损",
                    "asset": name,
                    "message": f"{name} 跌破 MA60！系统性风险信号，建议清仓",
                    "action": "清仓",
                })

    return alerts


# ============================================================
# 【新增】操盘手交易计划风控审查
# ============================================================


def load_trade_plan(trade_plan_path: str = None) -> dict:
    """读取操盘手的交易计划原始数据"""
    if trade_plan_path:
        return load_json(trade_plan_path)

    # 自动查找最新交易计划
    raw_dir = os.path.join(os.path.dirname(__file__), "..", "..", "data", "raw")
    pattern = os.path.join(raw_dir, "交易原始数据_*.json")
    files = sorted(glob.glob(pattern), reverse=True)
    if not files:
        return {}
    return load_json(files[0])


def risk_assess_trade(trade_item: dict, market_env: dict, portfolio: dict, current_position: float = 0) -> dict:
    """对单笔交易（买入/卖出）进行风险审查"""
    result = {
        "verdict": "APPROVED",    # APPROVED / REJECTED / CONDITIONAL
        "risk_factors": [],
        "conditions": [],
        "reason": "",
    }

    # === 买入审查 ===
    action = (trade_item.get("action") or "").lower()
    if action.startswith("买入") or action == "buy":
        result["type"] = "买入审查"

        # 1. 大盘环境检查
        env_level = market_env.get("level", "")
        if env_level == "extreme":
            result["verdict"] = "REJECTED"
            result["risk_factors"].append("极端行情，禁止任何买入")
        elif env_level == "bear":
            result["verdict"] = "CONDITIONAL"
            result["risk_factors"].append("弱市，买入需满足仓位<50%且单票≤10%")

        # 2. 仓位冲突检查（防御 null）
        max_position = market_env.get("max_position", 80)
        max_single = market_env.get("max_single", 20)
        suggested_pos = trade_item.get("suggested_position_pct") or 0
        if suggested_pos > max_single:
            result["verdict"] = "REJECTED"
            result["risk_factors"].append(
                f"建议仓位 {suggested_pos}% 超过单票上限 {max_single}%"
            )

        if current_position > max_position * 0.9:
            result["verdict"] = "CONDITIONAL"
            result["risk_factors"].append(
                f"当前仓位 {current_position:.0f}% 接近上限 {max_position}%，买入需先减仓"
            )

        # 3. 止损检查（防御 null）
        stop_loss = trade_item.get("stop_loss") or 0
        current_price = trade_item.get("current_price") or 0
        if stop_loss == 0 or current_price == 0:
            result["verdict"] = "REJECTED"
            result["risk_factors"].append("缺少止损位，不可买入")
        else:
            stop_pct = (stop_loss - current_price) / current_price * 100
            if stop_pct > -3:
                result["verdict"] = "CONDITIONAL"
                result["risk_factors"].append(
                    f"止损位过紧（{stop_pct:.1f}%），容易被噪音触发"
                )
                result["conditions"].append(f"建议放宽止损至-5%~-7%")

        # 4. 行业集中度（按持仓列表统计同代码前缀的数量，作为近似行业分散提示）
        code = trade_item.get("code", "")
        holdings = portfolio.get("持仓列表", [])
        same_prefix_count = sum(1 for h in holdings if h.get("代码", "")[:3] == code[:3] and h.get("代码", "") != code)
        if same_prefix_count >= 2:
            result["verdict"] = "CONDITIONAL"
            result["risk_factors"].append(
                f"同板块前缀已持有{same_prefix_count}只，注意行业集中风险"
            )
            result["conditions"].append("建议降低该行业总仓位不超过40%")

    # === 卖出审查 ===
    elif action.startswith("卖出") or action == "sell":
        result["type"] = "卖出审查"

        priority = trade_item.get("priority", "")
        reason = trade_item.get("reason", "")

        if "止损" in reason:
            # 止损卖出 - 自动批准
            result["verdict"] = "APPROVED"
            result["reason"] = "止损为硬规则，自动批准"
        elif "止盈" in reason:
            # 止盈卖出 - 检查是否过早
            pnl = trade_item.get("pnl_pct", 0)
            if pnl < 10:
                result["verdict"] = "CONDITIONAL"
                result["risk_factors"].append(f"盈利仅{pnl:.0f}%，建议等待+15%目标")
                result["conditions"].append("建议改为部分止盈（1/3仓）而非全部卖出")
            else:
                result["verdict"] = "APPROVED"
        else:
            result["verdict"] = "APPROVED"

    return result


def risk_assess_trade_plan(trade_plan: dict, market_env: dict, portfolio: dict, current_position: float = 0) -> dict:
    """审查操盘手整个交易计划"""
    buy_assessments = []
    for buy_item in trade_plan.get("buy_plan", []):
        assessment = risk_assess_trade(buy_item, market_env, portfolio, current_position)
        buy_assessments.append({
            "code": buy_item.get("code", ""),
            "name": buy_item.get("name", ""),
            "suggested_position": buy_item.get("suggested_position_pct", 0),
            "current_price": buy_item.get("current_price", 0),
            "stop_loss": buy_item.get("stop_loss", 0),
            "verdict": assessment["verdict"],
            "risk_factors": assessment.get("risk_factors", []),
            "conditions": assessment.get("conditions", []),
        })

    sell_assessments = []
    for sell_item in trade_plan.get("sell_plan", []):
        assessment = risk_assess_trade(sell_item, market_env, portfolio, current_position)
        sell_assessments.append({
            "code": sell_item.get("code", ""),
            "name": sell_item.get("name", ""),
            "reason": sell_item.get("reason", ""),
            "pnl_pct": sell_item.get("pnl_pct", 0),
            "verdict": assessment["verdict"],
            "risk_factors": assessment.get("risk_factors", []),
            "conditions": assessment.get("conditions", []),
        })

    return {
        "buy_assessments": buy_assessments,
        "sell_assessments": sell_assessments,
        "summary": {
            "approved_buys": len([b for b in buy_assessments if b["verdict"] == "APPROVED"]),
            "conditional_buys": len([b for b in buy_assessments if b["verdict"] == "CONDITIONAL"]),
            "rejected_buys": len([b for b in buy_assessments if b["verdict"] == "REJECTED"]),
            "approved_sells": len([s for s in sell_assessments if s["verdict"] == "APPROVED"]),
            "conditional_sells": len([s for s in sell_assessments if s["verdict"] == "CONDITIONAL"]),
            "rejected_sells": len([s for s in sell_assessments if s["verdict"] == "REJECTED"]),
        },
    }


def suggest_position_adjustment(
    portfolio: dict, market_env: dict, trade_plan: dict = None
) -> dict:
    """基于风控视角，给出持仓加减仓建议（与操盘手独立对比）"""
    suggestions = []
    holdings = portfolio.get("持仓列表", [])
    total_asset = _get_total_asset(portfolio)
    max_single = market_env.get("max_single", 20)

    for h in holdings:
        code = h.get("代码", "")
        if code == "000000" or not code:
            continue
        name = h.get("名称", "未知")
        cost = h.get("成本价", 0)
        market_value = h.get("市值", 0) or h.get("持股数量", 0) * h.get("当前价", 0)
        pnl_pct = h.get("当前盈亏%", 0)
        single_pct = (market_value / total_asset * 100) if total_asset > 0 else 0

        adj = {"code": code, "name": name, "action": "持有", "reason": "", "priority": "低"}

        # 加减仓条件判断（按优先级从高到低：需减仓 > 建议减仓 > 部分止盈 > 可加仓）
        if pnl_pct <= -5:
            adj["action"] = "建议减仓"
            adj["reason"] = f"浮亏{pnl_pct:.1f}%，接近-7%止损线，建议提前减仓"
            adj["priority"] = "高"
        elif single_pct > max_single * 1.2:
            adj["action"] = "需减仓"
            adj["reason"] = f"仓位{single_pct:.0f}%超单票上限{max_single}%，必须减至合规"
            adj["priority"] = "高"
        elif pnl_pct >= 15:
            adj["action"] = "部分止盈"
            adj["reason"] = f"浮盈{pnl_pct:.1f}%已达标，建议止盈1/3"
            adj["priority"] = "中"
        elif -3 <= pnl_pct <= 5 and single_pct < max_single * 0.5:
            adj["action"] = "可加仓"
            adj["reason"] = f"浮盈/亏可控({pnl_pct:.1f}%)，仓位未饱和({single_pct:.0f}%<{max_single}%上限的一半)"
            adj["priority"] = "中"

        suggestions.append(adj)

    return {
        "suggestions": suggestions,
        "note": "这些建议基于风控规则独立判断，可能与操盘手的交易计划不同。分歧处见冲突检测。",
    }


def detect_trade_conflicts(
    risk_assessment: dict, trade_plan: dict, position_suggestions: dict
) -> list:
    """检测风控官与操盘手的意见分歧，供投资领导仲裁"""
    conflicts = []
    trade_plan_buys = trade_plan.get("buy_plan", [])
    risk_buys = risk_assessment.get("buy_assessments", [])

    # 1. 买入分歧：风控否决但操盘手建议买入
    for rb in risk_buys:
        if rb["verdict"] == "REJECTED":
            tp_match = next(
                (tp for tp in trade_plan_buys if tp.get("code") == rb["code"]),
                None
            )
            if tp_match:
                conflicts.append({
                    "type": "风控否决买入",
                    "code": rb["code"],
                    "name": rb["name"],
                    "severity": "HIGH",
                    "risk_view": f"否决（{'；'.join(rb['risk_factors'])}）",
                    "trader_view": (
                        f"建议买入{tp_match.get('suggested_position_pct', '?')}%仓位"
                    ),
                    "arbitration_required": True,
                })

    # 2. 加减仓分歧：风控建议减仓但操盘手建议持有/买入
    for suggestion in position_suggestions.get("suggestions", []):
        if suggestion["action"] in ("建议减仓", "需减仓", "部分止盈"):
            code = suggestion["code"]
            # 检查操盘手是否在买入该票
            tp_buy = next(
                (tp for tp in trade_plan_buys if tp.get("code") == code),
                None
            )
            if tp_buy:
                conflicts.append({
                    "type": "加减仓分歧",
                    "code": code,
                    "name": suggestion["name"],
                    "severity": "HIGH" if suggestion["priority"] == "高" else "MEDIUM",
                    "risk_view": f"建议{suggestion['action']}（{suggestion['reason']}）",
                    "trader_view": (
                        f"建议买入{tp_buy.get('suggested_position_pct', '?')}%仓位"
                    ),
                    "arbitration_required": True,
                })

    # 3. 止盈分歧：风控建议部分止盈，操盘手还继续持有（未在卖出清单）
    for suggestion in position_suggestions.get("suggestions", []):
        if suggestion["action"] == "部分止盈":
            code = suggestion["code"]
            trade_sells = trade_plan.get("sell_plan", [])
            tp_sell = next(
                (tp for tp in trade_sells if tp.get("code") == code),
                None
            )
            if not tp_sell:
                conflicts.append({
                    "type": "止盈分歧",
                    "code": code,
                    "name": suggestion["name"],
                    "severity": "MEDIUM",
                    "risk_view": f"建议部分止盈（{suggestion['reason']}）",
                    "trader_view": "建议继续持有，未列入卖出计划",
                    "arbitration_required": True,
                })

    return conflicts


# ============================================================
# 主函数
# ============================================================


def generate_risk_report(portfolio_path: str = None, env_score: int = None, trade_plan_path: str = None) -> dict:
    """主函数：生成完整风控报告"""
    print("[风控官] 开始风险评估...")
    ok = "[OK]"
    warn = "[WARN]"
    fail = "[FAIL]"

    report = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "environment": {"score": env_score, "assessment": "未知", "max_position": 80, "max_single": 20},
        "portfolio_summary": {"total_asset": 0, "total_position": 0, "holding_count": 0},
        "alerts": [],
        "risk_level": "LOW",
        "suggested_action": "无操作",
        "errors": [],
    }

    # 1. 加载配置
    portfolio = load_portfolio(portfolio_path)
    holdings = portfolio.get("持仓列表", [])
    report["portfolio_summary"]["total_asset"] = _get_total_asset(portfolio)
    report["portfolio_summary"]["holding_count"] = len([h for h in holdings if h.get("代码") != "000000"])

    market_rules = load_position_rules()

    # 2. 如果没有传入环境评分，尝试从分析报告中读取
    if env_score is None:
        env_score = load_env_score_from_analysis()
        print(f"  {ok} 从分析报告读取环境评分: {env_score}")

    # 3. 确定市场环境
    market_env = determine_market_environment(env_score)
    report["environment"] = {
        "score": env_score,
        "assessment": market_env["name"],
        "max_position": market_env["max_position"],
        "max_single": market_env["max_single"],
    }
    print(f"  {ok} 市场环境: {market_env['name']} (上限: {market_env['max_position']}%)")

    # 4. 大盘环境检查（含指数均线联动止损）
    try:
        # 加载指数分析数据用于大盘联动止损判断
        index_data = load_index_analysis()
        env_alerts = check_market_risk(env_score, index_data)
        report["alerts"].extend(env_alerts)
        for a in env_alerts:
            print(f"  [{a['level']}] {a['type']}: {a['message']}")
    except Exception as e:
        report["errors"].append(f"大盘环境检查失败: {e}")
        print(f"  {fail} 大盘环境检查: {e}")

    # 5. 检查每个持仓
    for holding in holdings:
        code = holding.get("代码", "")
        name = holding.get("名称", "")
        if code == "000000" or not code:
            continue

        try:
            # 获取最新价格
            price_data = fetch_stock_price(code)
            if price_data:
                current_price = price_data["price"]
                pct_chg = price_data["pct_chg"]
                holding["当前价"] = current_price

                # 止损检查（传入ts_code用于获取历史最高价）
                stop_loss_alerts = check_stop_loss(holding, current_price, pct_chg, ts_code=code)
                report["alerts"].extend(stop_loss_alerts)
                for a in stop_loss_alerts:
                    print(f"  [{a['level']}] {a['type']} {a['asset']}: {a['message']}")
            else:
                print(f"  {warn} {name}({code}) 无法获取行情")
        except Exception as e:
            report["errors"].append(f"{name}({code}) 检查失败: {e}")
            print(f"  {fail} {name}: {e}")

    # 6. 仓位检查
    try:
        pos_alerts = check_position_limits(portfolio, market_env)
        report["alerts"].extend(pos_alerts)
        for a in pos_alerts:
            print(f"  [{a['level']}] {a['type']}: {a['message']}")
    except Exception as e:
        report["errors"].append(f"仓位检查失败: {e}")
        print(f"  {fail} 仓位检查: {e}")

    # 7. 【新增】审查操盘手交易计划
    trade_plan = load_trade_plan(trade_plan_path)
    trade_assessment = {}
    position_suggestions = {}
    trade_conflicts = []

    if trade_plan and (trade_plan.get("buy_plan") or trade_plan.get("sell_plan")):
        print(f"  {ok} 发现操盘手交易计划，开始风控审查...")
        # 计算当前总仓位百分比
        total_asset = _get_total_asset(portfolio)
        total_market_value = sum(
            h.get("市值", 0) or h.get("持股数量", 0) * h.get("当前价", 0)
            for h in portfolio.get("持仓列表", [])
        )
        current_position_pct = (total_market_value / total_asset * 100) if total_asset > 0 else 0

        trade_assessment = risk_assess_trade_plan(trade_plan, market_env, portfolio, current_position_pct)
        report["trade_plan_assessment"] = trade_assessment

        summary = trade_assessment.get("summary", {})
        print(f"    🟢 买入批准: {summary.get('approved_buys', 0)} 项")
        print(f"    🟡 买入有条件: {summary.get('conditional_buys', 0)} 项")
        print(f"    🔴 买入否决: {summary.get('rejected_buys', 0)} 项")
        print(f"    🟢 卖出批准: {summary.get('approved_sells', 0)} 项")
        print(f"    🟡 卖出有条件: {summary.get('conditional_sells', 0)} 项")
        print(f"    🔴 卖出否决: {summary.get('rejected_sells', 0)} 项")

        # 8. 【新增】持仓加减仓独立建议
        position_suggestions = suggest_position_adjustment(portfolio, market_env, trade_plan)
        report["position_suggestions"] = position_suggestions
        print(f"  {ok} 持仓调整建议完成")
        for s in position_suggestions.get("suggestions", []):
            print(f"    {s['action']}: {s['name']}({s['code']}) — {s['reason']}")

        # 9. 【新增】检测与操盘手的冲突
        trade_conflicts = detect_trade_conflicts(trade_assessment, trade_plan, position_suggestions)
        report["trade_conflicts"] = trade_conflicts
        if trade_conflicts:
            print(f"  {warn} 检测到 {len(trade_conflicts)} 项与操盘手的分歧!")
            for c in trade_conflicts:
                print(f"    [{c['severity']}] {c['type']}: {c['name']}({c['code']})")
                print(f"      风控: {c['risk_view']}")
                print(f"      操盘: {c['trader_view']}")
        else:
            print(f"  {ok} 与操盘手无意见分歧")
    else:
        print(f"  {warn} 未发现操盘手交易计划，跳过审查")

    # 10. 综合风险等级（含与操盘手冲突）
    critical_count = sum(1 for a in report["alerts"] if a["level"] == "CRITICAL")
    warning_count = sum(1 for a in report["alerts"] if a["level"] == "WARNING")
    rejected_count = len(trade_conflicts)

    if critical_count > 0:
        report["risk_level"] = "HIGH"
        report["suggested_action"] = "立即执行风控措施"
    elif warning_count > 0 or rejected_count > 0:
        report["risk_level"] = "MEDIUM"
        report["suggested_action"] = "关注预警项，准备应对"
    else:
        report["risk_level"] = "LOW"
        report["suggested_action"] = "维持当前操作"

    # 如有与操盘手的冲突，标记需要投资领导介入
    if trade_conflicts:
        report["needs_leadership"] = True
        report["leadership_note"] = (
            f"检测到 {len(trade_conflicts)} 项与操盘手的意见分歧，"
            "请投资领导（Agent7）仲裁后发布最终指令"
        )
    else:
        report["needs_leadership"] = False

    return report


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="风控检查")
    parser.add_argument("--portfolio", default=None, help="持仓JSON路径")
    parser.add_argument("--env-score", type=int, default=None, help="大盘环境评分(0-100)")
    parser.add_argument("--trade-plan", dest="trade_plan", default=None, help="交易计划原始数据JSON路径")
    args = parser.parse_args()

    report = generate_risk_report(args.portfolio, args.env_score, args.trade_plan)

    print("\n=== RESULT_JSON ===")
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    print("=== END ===")

    # 保存报告
    output_dir = os.path.join(os.path.dirname(__file__), "..", "..", "data", "raw")
    os.makedirs(output_dir, exist_ok=True)
    today = datetime.now().strftime("%Y%m%d")
    output_path = os.path.join(output_dir, f"风控报告_{today}.json")
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n风控报告已保存: {output_path}")
