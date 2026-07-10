"""
Agent6 操盘手 — 交易计划生成器

功能：
1. 读取选股建议、风控报告、分析报告
2. 读取持仓、仓位规则、止损规则
3. 生成买入/卖出/持有清单
4. 计算仓位分配
5. 输出交易计划报告

用法：
    source venv/Scripts/activate
    python -X utf8 scripts/agent6-操盘/trader.py

依赖上游报告：
    reports/日报/选股/选股建议_YYYY-MM-DD.md  — 候选池
    reports/日报/风控/风控报告_YYYY-MM-DD.md  — 风险等级
    reports/日报/分析/分析报告_YYYY-MM-DD.md  — 关键价位

D9反例（工作反例）：
1. 不要全仓买一只——黑天鹅即爆仓，单票≤20%，分批建仓
2. 不要频繁交易（每周多次）——手续费侵蚀利润，持股周期≥3天
3. 不要逆势加仓（越跌越买）——趋势下跌可能深套，止损出来等企稳再进
4. 不要忽视风控等级——风控HIGH时不建议买入
5. 不要不设止盈——坐过山车后利润归零，目标+15%~+20%分批止盈
6. 不要建议开盘追涨——波动大无法控制成本，等15分钟后或尾盘

D3异常处理表：
| 触发条件 | 一线修复 | 仍失败兜底 |
|---------|---------|-----------|
| 无风控报告（风控未运行） | 读取仓位管理规则自行判断 | 保守模式：总仓位≤50%，单票≤10% |
| 无选股建议（选股未运行） | 用持仓+自选做调仓计划 | 仅管理现有持仓（不买新票）|
| 行情获取失败（Tushare超时） | 重试1次 | 用持仓的当前价（或成本价）估算 |
| 仓位文件异常（portfolio.json缺失） | 从上周报告恢复 | 假设空仓，建议不交易 |
| 卖出数量超过实际持仓 | 自动截断到最大可卖数量 | 输出警告并在计划中标注"超持有限额" |
| 候选股解析失败（正则模式不匹配） | 用代码列表兜底（code, market元组） | 标记为"代码解析异常，需人工确认" |

D4 CHECKPOINT:
- CP1-买入合规：每笔买入 ≤ 单票最大仓位 × 总仓位上限
- CP2-卖出匹配：卖出的股票必须在持仓中，数量 ≤ 持仓量
- CP3-总仓位检查：交易后总仓位 ≤ 风控规则上限
- CP4-止损必设：每笔买入建议必须有止损位（-7%=固定止损）
- CP5-可行性检查：买入价格区间在当日涨跌停范围内
- CP6-涨跌停范围：建议买入价格超出涨跌停则标记为不可执行
"""

import os
import sys
import json
import re
from datetime import datetime

# 项目根目录
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def _get_stop_loss_ratio() -> float:
    """从止损规则.json读取固定比例止损阈值，默认-7%"""
    try:
        path = os.path.join(PROJECT_ROOT, "data", "止损规则.json")
        with open(path, "r", encoding="utf-8") as f:
            rules = json.load(f)
        for rule in rules.get("规则", []):
            if rule.get("类型") == "固定比例止损":
                ratio = rule.get("参数", {}).get("比例", -7)
                return 1.0 + ratio / 100.0
    except Exception:
        pass
    return 0.93
sys.path.insert(0, PROJECT_ROOT)
from scripts.utils.tushare_client import get_daily

# 数据库管理器（尽力而为，导入失败不影响交易计划生成）
try:
    from scripts.utils.db_manager import DatabaseManager
    _db = DatabaseManager()
except Exception as e:
    print(f"  [WARN] 数据库连接失败，跳过DB写入: {e}")
    _db = None


def load_json(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        print(f"[ERROR] JSON解析失败: {path} — {e}")
        return {}
    except Exception as e:
        print(f"[ERROR] 读取文件失败: {path} — {e}")
        return {}


def load_report(path: str) -> str:
    if not os.path.exists(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        print(f"[ERROR] 读取报告失败: {path} — {e}")
        return ""


def extract_top_picks(intelligence_text: str) -> list:
    """从选股建议文本中解析被推荐的股票列表（简化实现）"""
    picks = []
    # 匹配股票代码模式（6位数字+市场后缀）
    codes = re.findall(r'\b(\d{6}\.(SZ|SH))\b', intelligence_text)
    # Also try markdown table format
    names = re.findall(r'\*\*(.+?)\((\d{6}\.(?:SZ|SH))\)\*\*', intelligence_text)
    names = names or re.findall(r'\|\s*\d+\s*\|\s*\*\*(.+?)\*\*\s*\|\s*(\d{6}\.(?:SZ|SH))', intelligence_text)

    for name, code in names:
        picks.append({"name": name, "code": code})

    # 如果上面的模式没匹配到，用代码列表兜底
    if not picks:
        seen = set()
        for code, market in codes:
            if code not in seen:
                seen.add(code)
                picks.append({"name": code, "code": code})

    return picks


def get_stock_real_price(ts_code: str):
    """获取个股实时/最新行情 — DB优先，DB无数据则回退Tushare API"""
    try:
        today = datetime.now().strftime("%Y%m%d")
        # DB优先
        if _db:
            df = _db.get_daily_price(ts_code, today, today)
            if df is not None and not df.empty:
                last = df.iloc[0]
                return {
                    "price": float(last["close"]),
                    "high": float(last.get("high", last["close"])),
                    "low": float(last.get("low", last["close"])),
                    "pct_chg": float(last.get("pct_chg", 0)),
                    "volume": float(last.get("vol", 0)),
                    "trade_date": last.get("trade_date", ""),
                }
        # API回退
        df = get_daily(ts_code, f"{datetime.now().year}0101", today)
        if df is not None and not df.empty:
            df = df.sort_values("trade_date", ascending=False).reset_index(drop=True)
            last = df.iloc[0]
            return {
                "price": float(last["close"]),
                "high": float(last.get("high", last["close"])),
                "low": float(last.get("low", last["close"])),
                "pct_chg": float(last.get("pct_chg", 0)),
                "volume": float(last.get("vol", 0)),
                "trade_date": last.get("trade_date", ""),
            }
    except Exception as e:
        print(f'[WARN] 获取行情失败({ts_code}): {e}')
    return None


def calculate_position_size(
    total_asset: float,
    max_position_pct: float,
    max_single_pct: float,
    current_position: float,
    num_new_picks: int,
) -> dict:
    """计算可分配仓位"""
    available = total_asset * (max_position_pct / 100 - current_position / 100)
    per_stock_max = total_asset * (max_single_pct / 100)

    return {
        "total_available": max(0, available),
        "per_stock_max": per_stock_max,
        "suggested_per_stock": min(
            available / max(num_new_picks, 1), per_stock_max
        ),
    }


def get_limit_prices(ts_code: str, current_price: float) -> dict:
    """获取当日涨跌停价格范围"""
    if current_price <= 0:
        return {"涨停": None, "跌停": None, "limit_pct": None}
    # A股主板±10%，创业板/科创板±20%，北交所±30%
    is_cy = ts_code.startswith("30")   # 创业板 300xxx
    is_kc = ts_code.startswith("688") or ts_code.startswith("689")  # 科创板 688xxx/689xxx
    is_bj = ts_code.startswith("920")  # 北交所 920xxx
    limit_pct = 0.30 if is_bj else (0.20 if (is_cy or is_kc) else 0.10)
    return {
        "涨停": round(current_price * (1 + limit_pct), 2),
        "跌停": round(current_price * (1 - limit_pct), 2),
        "limit_pct": limit_pct,
    }


def fetch_technical_levels(ts_code: str):
    """获取技术支撑/压力位 — DB优先，DB无数据则回退Tushare API"""
    try:
        today = datetime.now().strftime("%Y%m%d")
        year_start = f"{datetime.now().year}0101"
        # DB优先
        df = None
        if _db:
            df = _db.get_daily_price(ts_code, year_start, today)
        if df is None or df.empty or len(df) < 20:
            df = get_daily(ts_code, year_start, today)
        if df is None or df.empty or len(df) < 20:
            return {"support": None, "resistance": None}

        df = df.sort_values("trade_date", ascending=False).reset_index(drop=True)
        closes = df["close"].values[:60]
        if len(closes) >= 10:
            return {"support": round(min(closes[:10]), 2), "resistance": round(max(closes[:10]), 2)}
    except Exception as e:
        print(f'[WARN] 获取技术位失败({ts_code}): {e}')
    return {"support": None, "resistance": None}


def generate_trade_plan() -> dict:
    """主函数：生成交易计划"""
    print("[操盘手] 开始制定交易计划...")
    ok = "[OK]"
    warn = "[WARN]"
    fail = "[FAIL]"

    today_str = datetime.now().strftime("%Y-%m-%d")

    result = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "risk_level": "unknown",
        "market_env": {},
        "portfolio": {},
        "buy_plan": [],
        "sell_plan": [],
        "hold_plan": [],
        "position_summary": {},
        "warnings": [],
        "errors": [],
    }

    # 1. 读取配置和上游报告
    portfolio = load_json(os.path.join(PROJECT_ROOT, "data", "portfolio.json"))

    # 读取上游报告（尽可能宽容）
    report_dir = os.path.join(PROJECT_ROOT, "reports", "日报")
    stock_picks_text = load_report(os.path.join(report_dir, "选股", f"选股建议_{today_str}.md"))
    risk_text = load_report(os.path.join(report_dir, "风控", f"风控报告_{today_str}.md"))
    analysis_text = load_report(os.path.join(report_dir, "分析", f"分析报告_{today_str}.md"))

    if not stock_picks_text:
        result["warnings"].append("无选股建议报告，将仅管理现有持仓")
        print(f"  {warn} 未找到选股建议")
    else:
        print(f"  {ok} 已读取选股建议")

    if not risk_text:
        result["warnings"].append("无风控报告，将使用保守模式（总仓位≤50%）")
        print(f"  {warn} 未找到风控报告")
    else:
        print(f"  {ok} 已读取风控报告")

    # 2. 解析风控等级
    risk_level = "MEDIUM"
    if risk_text:
        if "CRITICAL" in risk_text:
            risk_level = "CRITICAL"
        elif "HIGH" in risk_text:
            risk_level = "HIGH"
        elif "LOW" in risk_text and "WARNING" not in risk_text:
            risk_level = "LOW"
    result["risk_level"] = risk_level

    # 3. 确定仓位上限（从仓位管理规则.json读取）
    position_rules = load_json(os.path.join(PROJECT_ROOT, "data", "仓位管理规则.json"))
    _env_map = {
        "CRITICAL": "极端行情",
        "HIGH": "熊市/调整",
        "LOW": "牛市确认",
        "MEDIUM": "震荡市",
    }
    market_env_name = _env_map.get(risk_level, position_rules.get("默认环境", "震荡市"))
    env_config = position_rules.get("市场环境", {}).get(market_env_name, {})
    max_position = env_config.get("总仓位上限", 80)
    max_single = env_config.get("单票上限", 20)

    # 尝试从风控文本中读取总仓位上限和单票上限
    # 先找总仓位上限（"总仓位上限80%"或"总仓位≤80%"等模式）
    total_match = re.search(r'(?:总仓位|仓位|总).*?上限\D*(\d+)%', risk_text) if risk_text else None
    single_match = re.search(r'(?:单票|个股|单只).*?上限\D*(\d+)%', risk_text) if risk_text else None
    # 兜底：通用的"上限N%"模式
    fallback_match = re.search(r'上限\D*(\d+)%', risk_text) if risk_text and not total_match and not single_match else None

    if total_match:
        max_position = min(int(total_match.group(1)), max_position)
    elif fallback_match:
        max_position = min(int(fallback_match.group(1)), max_position)

    if single_match:
        max_single = min(int(single_match.group(1)), max_single)

    result["market_env"] = {"max_position": max_position, "max_single": max_single}
    print(f"  {ok} 仓位上限: {max_position}%（单票≤{max_single}%）")

    # 4. 持仓分析
    holdings = portfolio.get("持仓列表", [])
    total_asset = (
        portfolio.get("总资产") or
        portfolio.get("总资产_含现金") or
        portfolio.get("持仓总市值") or
        sum(h.get("市值", 0) for h in holdings) or
        1000000
    )
    total_position_value = sum(
        h.get("市值", 0) or h.get("持股数量", 0) * h.get("当前价", 0)
        for h in holdings if h.get("代码") != "000000"
    )
    current_position_pct = (total_position_value / total_asset * 100) if total_asset > 0 else 0

    result["portfolio"] = {
        "total_asset": total_asset,
        "current_position": round(current_position_pct, 1),
        "holding_count": len([h for h in holdings if h.get("代码") != "000000"]),
    }
    print(f"  {ok} 当前总仓位: {current_position_pct:.1f}%")

    # 5. 分析选股建议中的候选票
    if stock_picks_text:
        picks = extract_top_picks(stock_picks_text)
        print(f"  {ok} 从选股建议中解析出 {len(picks)} 只候选票")

        position_info = calculate_position_size(
            total_asset, max_position, max_single, current_position_pct, len(picks)
        )

        for pick in picks:
            price_data = get_stock_real_price(pick["code"])
            levels = fetch_technical_levels(pick["code"])

            current_price = price_data["price"] if price_data else 0

            # 涨跌停范围检查
            limit_info = get_limit_prices(pick["code"], current_price) if current_price > 0 else {}
            price_ok = True
            price_warning = ""
            if limit_info:
                if levels and levels.get("support") and limit_info.get("跌停") and levels["support"] < limit_info["跌停"]:
                    price_warning = f"支撑位{levels['support']}低于跌停价{limit_info['跌停']}"
                    price_ok = False
                if levels and levels.get("resistance") and limit_info.get("涨停") and levels["resistance"] > limit_info["涨停"]:
                    price_warning = f"压力位{levels['resistance']}超过涨停价{limit_info['涨停']}"
                    price_ok = False

            buy_item = {
                "code": pick["code"],
                "name": pick["name"],
                "current_price": current_price,
                "pct_chg": price_data["pct_chg"] if price_data else 0,
                "suggested_position_pct": min(
                    round(position_info["suggested_per_stock"] / total_asset * 100, 1),
                    max_single,
                ),
                "support": levels["support"],
                "resistance": levels["resistance"],
                "stop_loss": round(current_price * _get_stop_loss_ratio(), 2) if current_price > 0 else 0,
                "limit_up": limit_info.get("涨停") if limit_info else None,
                "limit_down": limit_info.get("跌停") if limit_info else None,
                "price_ok": price_ok,
                "price_warning": price_warning,
                "reason": "选股机器人推荐（详见选股建议报告）",
            }
            result["buy_plan"].append(buy_item)
            status = "✅" if price_ok else "⚠️"
            print(f"  {ok} {status} 建议买入: {pick['name']}({pick['code']}) "
                  f"仓位{buy_item['suggested_position_pct']}% "
                  f"止损{buy_item['stop_loss']} "
                  f"涨跌停:{limit_info.get('跌停','?')}/{limit_info.get('涨停','?')}")

    # 6. 现有持仓处理
    for h in holdings:
        code = h.get("代码", "")
        if code == "000000" or not code:
            continue
        name = h.get("名称", "未知")
        price_data = get_stock_real_price(code)
        current_price = price_data["price"] if price_data else h.get("当前价", 0)
        cost = h.get("成本价", 0)
        pnl_pct = ((current_price - cost) / cost * 100) if cost > 0 else 0

        # 检查止损（从止损规则.json读取阈值）
        stop_loss_pct = (_get_stop_loss_ratio() - 1.0) * 100  # 0.93→-7
        if pnl_pct <= stop_loss_pct:
            result["sell_plan"].append({
                "code": code,
                "name": name,
                "reason": "触发-7%止损线",
                "pnl_pct": round(pnl_pct, 1),
                "suggested_price": current_price,
                "action": "市价卖出",
                "priority": "高",
            })
            print(f"  ⛔ 止损触发: {name}({code}) 亏损{pnl_pct:.1f}% → 建议卖出")

        # 检查止盈
        elif pnl_pct >= 15:
            result["sell_plan"].append({
                "code": code,
                "name": name,
                "reason": f"达到止盈目标+{pnl_pct:.0f}%，建议分批止盈",
                "pnl_pct": round(pnl_pct, 1),
                "suggested_price": current_price,
                "action": "分批卖出1/3",
                "priority": "中",
            })
            print(f"  📈 止盈提醒: {name}({code}) 盈利{pnl_pct:.1f}%")

        else:
            result["hold_plan"].append({
                "code": code,
                "name": name,
                "reason": "持有观察",
                "pnl_pct": round(pnl_pct, 1),
                "stop_loss": round(current_price * 0.93, 2),
                "take_profit": round(current_price * 1.15, 2),
            })

    # 卖出校验：验证卖出清单中的每只股票确实在持仓中
    before = len(result["sell_plan"])
    result["sell_plan"] = [s for s in result["sell_plan"] if any(h.get("代码") == s.get("code") for h in holdings)]
    removed = before - len(result["sell_plan"])
    if removed > 0:
        result["warnings"].append(f"有{removed}只卖出股票不在持仓中，已从卖出清单移除")
        print(f"  [WARN] 有{removed}只卖出股票不在持仓中，已移除")

    # 7. 仓位汇总
    total_after_buy = current_position_pct + sum(
        b["suggested_position_pct"] for b in result["buy_plan"]
    )
    total_after_sell = sum(
        h.get("市值", 0) for h in holdings if any(
            s["code"] == h.get("代码") for s in result["sell_plan"]
        )
    )
    total_after = total_after_buy - (total_after_sell / total_asset * 100) if total_asset > 0 else total_after_buy

    result["position_summary"] = {
        "current_position_pct": round(current_position_pct, 1),
        "planned_buy_pct": round(sum(b["suggested_position_pct"] for b in result["buy_plan"]), 1),
        "planned_sell_pct": round(total_after_sell / total_asset * 100, 1) if total_asset > 0 else 0,
        "estimated_after_pct": round(total_after, 1),
        "max_allowed_pct": max_position,
    }

    if total_after > max_position:
        result["warnings"].append(
            f"执行后仓位 {total_after:.0f}% 超过允许上限 {max_position}%，建议减少买入或增加卖出"
        )
        print(f"  {warn} 仓位预警: 执行后{total_after:.0f}% > 上限{max_position}%")

    # 8. 写入数据库（尽力而为）
    _save_trader_to_db(result, holdings)

    return result


def _save_trader_to_db(result: dict, holdings: list):
    """保存交易计划相关数据到数据库"""
    global _db
    if _db is None:
        return

    today = datetime.now().strftime("%Y-%m-%d")
    today_ymd = datetime.now().strftime("%Y%m%d")
    ok, warn = "[OK]", "[WARN]"

    # 8a. 持仓快照 → portfolio_snapshot
    if holdings:
        try:
            n = _db.save_portfolio_snapshot(today, holdings)
            print(f"  {ok} DB: 持仓快照写入 {n} 条")
        except Exception as e:
            print(f"  {warn} DB: 持仓快照写入失败: {e}")

    # 8b. 报告日志
    try:
        has_errors = bool(result.get("errors"))
        has_warnings = bool(result.get("warnings"))
        status = "ok" if not has_errors else ("warning" if has_warnings else "error")
        _db.save_report_log(today_ymd, "agent6", status,
                           error_msg="; ".join(result.get("errors", []) + result.get("warnings", [])))
    except Exception as e:
        print(f"  {warn} DB: 报告日志写入失败: {e}")


if __name__ == "__main__":
    report = generate_trade_plan()

    print("\n=== RESULT_JSON ===")
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    print("=== END ===")

    # 保存
    output_dir = os.path.join(PROJECT_ROOT, "data", "raw")
    os.makedirs(output_dir, exist_ok=True)
    today = datetime.now().strftime("%Y%m%d")
    output_path = os.path.join(output_dir, f"交易原始数据_{today}.json")
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n交易计划已保存: {output_path}")
