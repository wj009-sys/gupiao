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

D3异常处理表:
| 触发条件 | 一线修复 | 仍失败兜底 |
|---------|---------|-----------|
| 行情数据为空(非交易日/停牌) | 重试1次，或往前推1天 | 该股票跳过评分，标注"数据不可用" |
| 停复牌检查失败(suspend_d接口异常) | 用常规daily接口判断当日是否有数据 | 标注"停牌状态未知"，保守不推荐 |
| 因子数据缺失(财务数据未更新) | 用前季数据代替，标注"数据滞后" | 该因子按0分处理，不影响其他因子 |
| 板块数据为空 | 跳过板块动量/热度因子 | 仅用技术面+基本面因子评分 |
| 盘中模式获取实时价失败 | 用最近日线收盘价代替 | 标注"非实时价" |
| 涨跌停价格超出范围 | 自动截断至涨跌停价以内 | 标记为"超出涨跌停，不可执行" |
| 持仓文件中无总资产字段 | 依次尝试总资产/总资产_含现金/持仓总市值 | 使用持仓市值之和+现金估算 |

D4 CHECKPOINT:
- CP1-模式匹配验证：输入mode必须匹配4种枚举值之一
- CP2-模式特有检查：pre_market必须有昨日行情；intraday必须有盘中数据
- CP3-停复牌检查：推荐标的必须排除当日停牌股票
- CP4-ST过滤：ST/*ST股票必须排除
- CP5-退市过滤：退市整理期股票必须排除
- CP6-因子独立性验证：成长因子不能与动量因子共用计算逻辑
- CP7-涨跌停范围：推荐价格必须在涨跌停范围内
- CP8-持仓冲突：推荐列表与现有持仓不重复

D9工作反例：
- 不要早盘用晚间权重——不同使用场景对应独立权重配置
- 不要盘中看估值——盘中数据不可用时跳过基本面因子
- 不要不看复盘偏差——evening模式必须读取复盘报告中的因子调整建议
- 不要推荐已持仓股票——检查与现有持仓的重复
- 不要用动量因子冒充成长因子——两因子独立计算
- 不要忽略OBV的累积特性——OBV绝对值无意义，只看方向和交叉信号
- 不要只看OBV不看价格——OBV必须与价格走势结合判断（顶背离/底背离）
- 不要用线性评分替代非线性曲线——许多因子（换手率/量比）的最佳区间是中间值而非越大越好
- 不要孤立评估单因子——同一风险维度需叠加惩罚而非忽略（如同时高动量+高换手→双重追高风险）
"""

# ============================================================
#  评分曲线配置（借鉴AlphaSift scoring_profile）
#  配置数据从 data/选股规则.json 的 scoring_profile 加载
# ============================================================

DEFAULT_SCORING_PROFILE = {
    "momentum_chase_start_pct": 12.0,        # 涨幅超12%开始惩罚追高
    "activity_ideal_volume_ratio": 1.8,       # 理想量比1.8
    "activity_ideal_turnover_rate": 5.0,      # 理想换手率5%
    "reversal_ideal_change_pct": -3.0,        # 偏好-3%的回撤修复
    "stability_hot_change_pct": 9.0,          # 超9%开始惩罚过热
    "stability_extreme_volume_ratio": 5.0,    # 量比>5视为极端
    "theme_heat_overheat_score": 80.0,        # 主题热度超80惩罚
    "theme_heat_persistence_min_score": 60.0,  # 主题持续性最低分
    "liquidity_min_amount": 5000.0,            # 最小日成交额(万元)
    "reversal_max_decline_pct": -15.0,         # 最大可接受回撤幅度
}

STOP_LOSS_RATIO = None  # lazy-loaded


def _get_stop_loss_ratio() -> float:
    """从止损规则.json读取固定比例止损阈值，默认-7%"""
    global STOP_LOSS_RATIO
    if STOP_LOSS_RATIO is not None:
        return STOP_LOSS_RATIO
    try:
        project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
        path = os.path.join(project_root, "data", "止损规则.json")
        with open(path, "r", encoding="utf-8") as f:
            rules = json.load(f)
        for rule in rules.get("规则", []):
            if rule.get("类型") == "固定比例止损":
                ratio = rule.get("参数", {}).get("比例", -7)
                STOP_LOSS_RATIO = 1.0 + ratio / 100.0
                return STOP_LOSS_RATIO
    except Exception:
        pass
    STOP_LOSS_RATIO = 0.93
    return STOP_LOSS_RATIO


def _load_scoring_profile(config: dict) -> dict:
    """从配置加载评分曲线参数，缺失项用默认值"""
    rules = config.get("rules", {})
    profile = rules.get("scoring_profile", {})
    merged = DEFAULT_SCORING_PROFILE.copy()
    merged.update(profile)
    return merged

def _penalty_for_overheating(value: float, threshold: float, max_penalty: float = 20) -> float:
    """
    非线性过热惩罚函数（借鉴AlphaSift评分曲线）

    当 value > threshold 时，惩罚随超出比例非线性增加。
    penalty = min(max_penalty, (value / threshold - 1) * max_penalty * 1.5)

    Args:
        value: 当前值
        threshold: 阈值
        max_penalty: 最大惩罚分
    Returns:
        惩罚分（0 ~ max_penalty）
    """
    if value <= threshold:
        return 0.0
    excess_ratio = value / threshold - 1
    penalty = min(max_penalty, excess_ratio * max_penalty * 1.5)
    return max(0, penalty)

def _score_ideal_middle(value: float, ideal: float, max_score: float = 100,
                        tolerance: float = 0.3, penalty: float = 10) -> float:
    """
    理想中间值评分函数：偏离ideal时降分（非对称钟形曲线）

    用于量比/换手率等"太高不好, 太低也不好"的因子。
    以 ideal 为中心，± tolerance 范围内满分，超出后线性降分。

    Args:
        value: 当前值
        ideal: 理想中心值
        max_score: 满分
        tolerance: 容忍范围（相对于ideal的比例）
        penalty: 每超 tolerance 一档的降分
    Returns:
        评分
    """
    if value <= 0:
        return max_score * 0.3  # 零值只给30%
    deviation = abs(value - ideal) / ideal
    if deviation <= tolerance:
        return max_score
    tiers = int((deviation - tolerance) / tolerance) + 1
    score = max_score - tiers * penalty
    return max(max_score * 0.3, min(max_score, score))

import os, sys, json
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from scripts.utils.tushare_client import pro

# RPS相对价格强度（导入失败不影响选股）
try:
    from scripts.utils import rps as rps_engine
    _has_rps = True
except Exception as e:
    print(f"  [WARN] RPS模块导入失败: {e}")
    _has_rps = False

# 数据库管理器（尽力而为，导入失败降级到纯API模式）
try:
    from scripts.utils.db_manager import DatabaseManager, get_daily_price_db_first
    _db = DatabaseManager()
except Exception as e:
    print(f"  [WARN] 数据库连接失败，使用纯API模式: {e}")
    _db = None


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
    except Exception as e:
        print(f"  ⚠️ 获取{ts_code}名称失败，回退为code: {e}")
    return ts_code


def is_st_stock(ts_code: str) -> bool:
    """检查是否为ST/*ST/退市股票"""
    try:
        df = pro.stock_basic(ts_code=ts_code, fields="ts_code,name")
        if df is not None and not df.empty:
            name = df.iloc[0].get("name", "")
            return "ST" in name or "退市" in name
    except Exception as e:
        print(f"  ⚠️ {ts_code} ST检查失败（默认不拦截）: {e}")
    return False


def is_suspended(ts_code: str, trade_date: str) -> bool:
    """检查股票是否停牌"""
    try:
        df = pro.suspend_d(ts_code=ts_code, suspend_date=trade_date)
        if df is not None and not df.empty:
            return True
    except Exception as e:
        print(f"  ⚠️ {ts_code} 停牌检查失败（默认视为未停牌）: {e}")
    return False


def get_stock_industry(ts_code: str) -> str:
    """查询股票所属行业（从DB或Tushare获取）"""
    try:
        df = pro.stock_basic(ts_code=ts_code, fields='ts_code,industry')
        if df is not None and not df.empty:
            ind = df.iloc[0].get('industry', '')
            return ind if ind else '未知'
    except Exception as e:
        print(f"  [WARN] 查询行业失败({ts_code}): {e}")
    return '未知'


def get_hot_sectors(intelligence_text: str, config: dict = None) -> list:
    """
    从情报摘要中提取热点板块方向

    关键词列表从 data/选股规则.json 的 "热点板块关键词" 加载，
    如未配置则使用内置默认关键词。
    借鉴 AlphaSift 的 theme_heat 热度追踪机制。
    """
    # 从配置加载（优先）
    if config:
        rules = config.get("rules", {})
        kw_config = rules.get("热点板块关键词", {})
        sectors = kw_config.get("关键词", [])
        default_hot = kw_config.get("默认热点", ["人工智能", "半导体", "新能源"])
    else:
        sectors = [
            "半导体", "芯片", "新能源", "光伏", "锂电池", "人工智能", "AI",
            "消费电子", "医药", "医疗", "金融", "券商", "银行", "保险",
            "房地产", "基建", "军工", "通信", "5G", "机器人", "低空经济",
            "无人驾驶", "量子计算", "数据要素", "信创", "鸿蒙",
        ]
        default_hot = ["人工智能", "半导体", "新能源"]

    hot = []
    if intelligence_text:
        for s in sectors:
            if s in intelligence_text:
                hot.append(s)
    if not hot:
        return default_hot
    return hot


def get_sector_stocks(sector_name: str, max_count: int = 20) -> list:
    """获取某个板块的成分股"""
    try:
        df = pro.ths_member(ts_code="", name=sector_name)
        if df is not None and not df.empty:
            return df["ts_code"].tolist()[:max_count]
    except Exception as e:
        print(f"  ⚠️ 获取板块{sector_name}成分股失败: {e}")
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
    # 注意：策略规则.json 为人工参考文件（信号定义），脚本暂不直接消费
    portfolio = load_json(os.path.join(root, "data", "portfolio.json"))
    watchlist = load_json(os.path.join(root, "data", "watchlist.json"))

    today = datetime.now().strftime("%Y-%m-%d")
    report_dir = os.path.join(root, "reports", "日报")
    intelligence = load_report(os.path.join(report_dir, "情报", f"情报摘要_{today}.md"))
    analysis = load_report(os.path.join(report_dir, "分析", f"分析报告_{today}.md"))
    review = load_report(os.path.join(report_dir, "复盘", f"复盘报告_{today}.md"))

    return {
        "rules": rules,
        "portfolio": portfolio,
        "watchlist": watchlist,
        "intelligence": intelligence,
        "analysis": analysis,
        "review": review,
    }


# ============================================================
#  公共评分函数（所有模式共用）
# ============================================================

# ============================================================
#  新增: AlphaSift 式因子 (流动性/稳定性/反转)
# ============================================================

def score_liquidity(ts_code: str, trade_date: str, profile: dict = None) -> dict:
    """
    流动性因子评分（0-100）— 借鉴AlphaSift factor_liquidity_score

    基于日成交额和换手率，使用非线性评分曲线：
    - 成交额越高越好（但用log平滑）
    - 换手率用理想中间值函数（太高=投机, 太低=僵尸）
    """
    if profile is None:
        profile = DEFAULT_SCORING_PROFILE
    try:
        # DB优先：从 daily_price 获取 amount，从 daily_basic 获取 turnover_rate
        db_amount = None
        db_turnover = None
        if _db:
            basic = _db.get_daily_basic(ts_code=ts_code, trade_date=trade_date)
            if basic:
                db_turnover = basic.get("turnover_rate")
            price = _db.get_daily_price(ts_code, trade_date, trade_date)
            if price is not None and not price.empty:
                db_amount = float(price.iloc[0].get("amount", 0))

        if db_amount is not None and db_turnover is not None:
            amount = db_amount / 10000  # 转为万元
            turnover = db_turnover
        else:
            # API回退
            df = pro.daily_basic(ts_code=ts_code, trade_date=trade_date)
            if df is None or df.empty:
                return {"score": 50, "details": {"reason": "无流动性数据，给中性分"}}
            row = df.iloc[0]
            amount = float(row.get("amount", 0)) / 10000
            turnover = float(row.get("turnover_rate", 0))

        # 成交额评分：log平滑（避免超大市值主导）
        min_amount = profile.get("liquidity_min_amount", 5000.0)
        if amount >= min_amount:
            # log(amount/min_amount) / log(100) * 50 + 50
            # 成交额达到min_amount给50分，100倍min_amount给100分
            amount_score = 50 + min(50, np.log2(amount / min_amount) / np.log2(100) * 50)
        else:
            amount_score = max(0, 50 * (amount / min_amount))

        # 换手率评分：理想中间值
        ideal_turnover = profile.get("activity_ideal_turnover_rate", 5.0)
        turnover_score = _score_ideal_middle(turnover, ideal_turnover,
                                             max_score=100, tolerance=0.5, penalty=15)

        # 综合流动性分（成交额权重60% + 换手率权重40%）
        liquidity_score = amount_score * 0.6 + turnover_score * 0.4
        liquidity_score = max(0, min(100, liquidity_score))

        detail = f"成交额={amount:.0f}万(分{amount_score:.0f}) 换手率={turnover:.1f}%(分{turnover_score:.0f})"
        return {"score": round(liquidity_score, 1), "details": {"reason": detail,
                 "amount": round(amount, 0), "turnover_rate": round(turnover, 2)}}
    except Exception as e:
        return {"score": 50, "details": {"reason": f"流动性评分异常: {e}"}}


def score_stability(ts_code: str, trade_date: str, profile: dict = None) -> dict:
    """
    稳定性因子评分（0-100）— 借鉴AlphaSift factor_stability_score

    惩罚极端波动和异常换手（过热惩罚）：
    - 单日涨跌幅过大 → 惩罚
    - 换手率过高（投机过重）→ 惩罚
    - 负PE → 惩罚
    """
    if profile is None:
        profile = DEFAULT_SCORING_PROFILE
    try:
        df = get_daily_price_db_first(ts_code, days_back=20)

        # DB优先：从 daily_basic 读取基本面数据
        basic_row = {}
        if _db:
            basic = _db.get_daily_basic(ts_code=ts_code, trade_date=trade_date)
            if basic:
                basic_row = basic
        if not basic_row:
            basic_df = pro.daily_basic(ts_code=ts_code, trade_date=trade_date)
            if basic_df is not None and not basic_df.empty:
                basic_row = basic_df.iloc[0].to_dict()

        score = 85  # 起始高分（从满分开始扣）
        details = []
        penalties = []

        # === 1. 价格波动惩罚 ===
        if df is not None and not df.empty and len(df) >= 10:
            pct_chgs = df["pct_chg"].values[:10]
            max_up = max(pct_chgs) if len(pct_chgs) > 0 else 0
            max_down = abs(min(pct_chgs)) if len(pct_chgs) > 0 else 0
            hot_threshold = profile.get("stability_hot_change_pct", 9.0)

            # 单日过大涨幅惩罚
            if max_up > hot_threshold:
                p = _penalty_for_overheating(max_up, hot_threshold, max_penalty=20)
                penalties.append(p)
                details.append(f"单日最大涨幅{max_up:.1f}%>阈值{hot_threshold}%，惩罚-{p:.0f}分")

            # 单日过大跌幅惩罚
            if max_down > hot_threshold:
                p = _penalty_for_overheating(max_down, hot_threshold, max_penalty=15)
                penalties.append(p)
                details.append(f"单日最大跌幅{max_down:.1f}%>阈值{hot_threshold}%，惩罚-{p:.0f}分")

            # 波动率惩罚（ATR/价格比例）
            closes = df["close"].values[:10]
            if len(closes) >= 5:
                atr = np.std(np.diff(closes))
                atr_ratio = atr / (np.mean(closes) + 1e-10)
                if atr_ratio > 0.05:  # 日波动>5%
                    vol_penalty = min(15, (atr_ratio - 0.05) * 200)
                    penalties.append(vol_penalty)
                    details.append(f"波动率{atr_ratio:.1%}>5%，惩罚-{vol_penalty:.0f}分")

        # === 2. 换手率过热惩罚 ===
        extreme_vol_ratio = profile.get("stability_extreme_volume_ratio", 5.0)
        if basic_row:
            turnover = float(basic_row.get("turnover_rate", 0))
            if turnover > extreme_vol_ratio:
                p = _penalty_for_overheating(turnover, extreme_vol_ratio, max_penalty=15)
                penalties.append(p)
                details.append(f"换手率{turnover:.1f}%>极端阈值{extreme_vol_ratio}%，惩罚-{p:.0f}分")

            # === 3. 负PE惩罚 ===
            pe = basic_row.get("pe")
            if pe is not None and pe < 0:
                penalties.append(20)
                details.append(f"PE={pe}<0(亏损)，惩罚-20分")

        # === 4. 量比异常惩罚 ===
        if basic_row:
            vol_ratio = float(basic_row.get("volume_ratio", 1))
            if vol_ratio > extreme_vol_ratio + 2:
                p = min(10, (vol_ratio - extreme_vol_ratio - 2) * 5)
                penalties.append(p)
                details.append(f"量比{vol_ratio:.1f}异常高，惩罚-{p:.0f}分")

        # 应用惩罚
        total_penalty = sum(penalties)
        final_score = max(0, min(100, score - total_penalty))

        detail_str = " | ".join(details) if details else "波动正常，无过热惩罚"
        return {"score": round(final_score, 1), "details": {"reason": detail_str,
                "penalties": round(total_penalty, 1), "factor_count": len(penalties)}}
    except Exception as e:
        return {"score": 65, "details": {"reason": f"稳定性评分异常，给中性偏正分: {e}"}}


def score_reversal(ts_code: str, trade_date: str, profile: dict = None) -> dict:
    """
    反转因子评分（0-100）— 借鉴AlphaSift factor_reversal_score

    寻找控制回撤后的修复机会：
    - 短期回撤幅度适中（-3%~-8%为最佳反转区域）
    - 回撤后出现企稳信号（RSI回升/缩量)
    - 回撤过大(-15%以下)则不参与
    """
    if profile is None:
        profile = DEFAULT_SCORING_PROFILE
    try:
        df = get_daily_price_db_first(ts_code, days_back=30)
        if df is None or df.empty or len(df) < 15:
            return {"score": 50, "details": {"reason": "数据不足"}}

        pct_chgs = df["pct_chg"].values
        closes = df["close"].values

        # 近10日累计涨跌幅
        recent_ret_10d = (closes[0] - closes[min(9, len(closes)-1)]) / closes[min(9, len(closes)-1)] * 100

        # 近5日最大回撤
        if len(pct_chgs) >= 5:
            max_drawdown_5d = min(pct_chgs[:5])
        else:
            max_drawdown_5d = min(pct_chgs)

        score = 50
        details = []

        ideal_change = profile.get("reversal_ideal_change_pct", -3.0)
        max_decline = profile.get("reversal_max_decline_pct", -15.0)

        # === 1. 回撤幅度评分 ===
        if max_drawdown_5d <= max_decline:
            # 回撤太大，不参与反转
            score = 20
            details.append(f"近5日最大回撤{max_drawdown_5d:.1f}%>阈值{abs(max_decline)}%，不参与")
        elif max_drawdown_5d <= ideal_change:
            # 回撤适中，有反转潜力
            # ideal_change = -3%, 越接近-3%分越高
            ratio = max_drawdown_5d / ideal_change  # e.g. -6%/-3% = 2
            if ratio <= 2:  # 回撤在-3%~-6%
                score = 75
                details.append(f"回撤{max_drawdown_5d:.1f}%适中，反转潜力高")
            elif ratio <= 3:
                score = 60
                details.append(f"回撤{max_drawdown_5d:.1f}%偏大，关注企稳信号")
            else:
                score = 40
                details.append(f"回撤{max_drawdown_5d:.1f}%过大，谨慎参与")
        else:
            # 回撤很小或是正收益
            score = 40
            details.append(f"近5日最大回撤仅{max_drawdown_5d:.1f}%，无明显反转机会")

        # === 2. 企稳确认（RSI从低位回升）===
        if len(closes) >= 14:
            try:
                # 用涨跌幅做简单判断（替代RSI计算）
                last_3d = pct_chgs[:3]
                if len(pct_chgs) >= 7:
                    early_3d = pct_chgs[4:7]
                    # 如果前跌后涨，企稳信号
                    if np.mean(early_3d) < -1 and np.mean(last_3d) > -0.5:
                        score += 15
                        details.append("后3日企稳(前跌后稳)")
                    elif np.mean(last_3d) < np.mean(early_3d) - 1:
                        score -= 10
                        details.append("仍在加速下跌")
            except Exception as e:
                print(f"  [WARN] 后3日企稳检查异常: {e}")

        # === 3. 缩量企稳加分 ===
        try:
            volumes = df["vol"].values if "vol" in df.columns else df["volume"].values if "volume" in df.columns else None
            if volumes is not None and len(volumes) >= 10:
                avg_vol_10d = np.mean(volumes[5:10])  # 10日均量
                avg_vol_3d = np.mean(volumes[:3])     # 近3日均量
                if avg_vol_10d > 0 and avg_vol_3d < avg_vol_10d * 0.8:
                    score += 10
                    details.append("缩量企稳")
        except Exception as e:
            print(f"  [WARN] 缩量企稳检查异常: {e}")

        final = max(0, min(100, score))
        return {"score": round(final, 1), "details": {"reason": "; ".join(details) if details else "中性无反转信号",
                 "max_drawdown_5d": round(max_drawdown_5d, 1), "recent_ret_10d": round(recent_ret_10d, 1)}}
    except Exception as e:
        return {"score": 50, "details": {"reason": f"反转评分异常: {e}"}}


def score_valuation(ts_code: str, trade_date: str) -> dict:
    """估值因子评分（0-100）- DB优先读取"""
    global _db

    # 尝试从数据库读取
    basic_data = None
    if _db:
        try:
            basic_data = _db.get_daily_basic(ts_code, trade_date)
        except Exception as e:
            print(f"  ⚠️ 读取{ts_code} daily_basic失败: {e}")

    # 数据库命中
    if basic_data and basic_data.get("pe") is not None:
        pe = basic_data.get("pe")
        pb = basic_data.get("pb")
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
        return {"score": max(0, min(100, score)), "details": details, "source": "db"}

    # 数据库未命中，回退到 Tushare API
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
        # 写入数据库
        try:
            if _db and df is not None and not df.empty:
                _db.upsert_daily_basic(df)
        except Exception as e:
            print(f"  ⚠️ 缓存{ts_code} basic数据失败: {e}")
        return {"score": max(0, min(100, score)), "details": details, "source": "api"}
    except Exception as e:
        print(f"  [WARN] 估值评分{ts_code}失败: {e}")
        return {"score": 50, "details": {"reason": "估值评分异常"}}


def score_momentum(ts_code: str) -> dict:
    """动量因子评分（0-100）- DB优先读取"""
    try:
        df = get_daily_price_db_first(ts_code, days_back=60)
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
    except Exception as e:
        print(f"  [WARN] 动量评分{ts_code}失败: {e}")
        return {"score": 50, "details": {"reason": "动量评分异常"}}


def score_technical(ts_code: str) -> dict:
    """技术面因子评分（0-100）- DB优先读取(daily_indicator) + OBV量能加分"""
    try:
        # === 路径A: 优先从 daily_indicator 缓存读取 ===
        indicator_data = _db.get_latest_indicator(ts_code) if _db else {}
        if indicator_data and indicator_data.get("ma_20") is not None:
            ma20 = indicator_data["ma_20"]
            # 需要从daily_price获取最新价格（indicator表没有价格）
            df_price = get_daily_price_db_first(ts_code, days_back=5)
            if df_price is not None and not df_price.empty:
                current = float(df_price.iloc[0]["close"])
                # MA20评分
                if current > ma20 * 1.05:
                    score = 80
                    detail = f"价格({current:.2f}) > MA20({ma20:.2f}) +5%"
                elif current > ma20:
                    score = 65
                    detail = f"价格({current:.2f}) > MA20({ma20:.2f})"
                else:
                    score = 35
                    detail = f"价格({current:.2f}) < MA20({ma20:.2f})"

                # 成交量比（从daily_price判断）
                vol_score = 0
                if df_price is not None and not df_price.empty and "vol" in df_price.columns:
                    vol_vals = df_price["vol"].values[:8]
                    if len(vol_vals) >= 8:
                        recent_avg = sum(vol_vals[:3]) / 3
                        prev_avg = sum(vol_vals[3:8]) / 5
                        if prev_avg > 0 and recent_avg > prev_avg * 1.3:
                            vol_score = 15
                            detail += "，放量"

                # OBV量能加分（从daily_indicator缓存读取）
                obv_bonus = 0
                obv_detail = ""
                obv = indicator_data.get("obv")
                obv_ma20 = indicator_data.get("obv_ma20")
                obv_trend = indicator_data.get("obv_trend", "")
                if obv is not None and obv_ma20 is not None:
                    if obv > obv_ma20:
                        obv_bonus += 10
                        obv_detail += "OBV>MA20(量能偏多)"
                    else:
                        obv_bonus -= 5
                        obv_detail += "OBV<MA20(量能偏空)"
                    if obv_trend == "上升":
                        obv_bonus += 5
                        obv_detail += "，量能上升"
                    elif obv_trend == "下降":
                        obv_bonus -= 5
                        obv_detail += "，量能下降"
                if obv_detail:
                    detail += f" | {obv_detail}"

                final_score = max(0, min(100, score + vol_score + obv_bonus))
                return {"score": final_score,
                        "details": {"summary": detail, "source": "daily_indicator"}}

        # === 路径B: daily_indicator无数据，fallback到自行计算 ===
        df = get_daily_price_db_first(ts_code, days_back=30)
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

        # === OBV 量能加分（fallback自算） ===
        obv_bonus = 0
        obv_detail = ""
        try:
            if volumes is not None and len(volumes) >= 10:
                close_asc = closes[::-1]
                vol_asc = volumes[::-1]
                obv_vals = np.zeros(len(close_asc))
                for i in range(1, len(close_asc)):
                    if close_asc[i] > close_asc[i - 1]:
                        obv_vals[i] = obv_vals[i - 1] + vol_asc[i]
                    elif close_asc[i] < close_asc[i - 1]:
                        obv_vals[i] = obv_vals[i - 1] - vol_asc[i]
                    else:
                        obv_vals[i] = obv_vals[i - 1]

                latest_obv = obv_vals[-1]
                if len(obv_vals) >= 10:
                    obv_ma10 = np.mean(obv_vals[-10:])
                    if latest_obv > obv_ma10:
                        obv_bonus += 10
                        obv_detail += "OBV>MA10(量能偏多)"
                    else:
                        obv_bonus -= 5
                        obv_detail += "OBV<MA10(量能偏空)"
                    if len(obv_vals) >= 15:
                        recent_obv_slope = obv_vals[-1] - obv_vals[-6]
                        if recent_obv_slope > 0:
                            obv_bonus += 5
                            obv_detail += "，量能上升"
                        elif recent_obv_slope < 0:
                            obv_bonus -= 5
                            obv_detail += "，量能下降"
                if obv_detail:
                    detail += f" | {obv_detail}"
        except Exception as e:
            print(f"  [WARN] OBV量能加分计算失败: {e}")

        final_score = max(0, min(100, score + vol_score + obv_bonus))
        return {"score": final_score,
                "details": {"summary": detail, "source": "self_calc"}}
    except Exception as e:
        print(f"  [WARN] 技术评分{ts_code}失败: {e}")
        return {"score": 50, "details": {"reason": "技术评分异常"}}


def score_sentiment(ts_code: str, trade_date: str = None) -> dict:
    """情绪因子评分（0-100）— DB优先(moneyflow_stock)→Tushare API→涨跌幅兜底"""
    if not trade_date:
        trade_date = datetime.now().strftime("%Y%m%d")

    # === 路径A: 优先从 moneyflow_stock DB读取 ===
    if _db:
        try:
            mf = _db.get_moneyflow_stock(ts_code, trade_date)
            if mf and mf.get("net_amount") is not None:
                net = float(mf["net_amount"])
                if net > 0:
                    return {"score": 65,
                            "details": {"reason": f"主力资金净流入{net:.0f}万", "source": "moneyflow_stock"}}
                elif net < 0:
                    return {"score": 40,
                            "details": {"reason": f"主力资金净流出{abs(net):.0f}万", "source": "moneyflow_stock"}}
        except Exception as e:
            print(f"  [WARN] {ts_code} moneyflow_stock DB读取失败: {e}")

    # === 路径B: Tushare API ===
    try:
        df = pro.moneyflow(ts_code=ts_code, start_date=trade_date, end_date=trade_date)
        if df is not None and not df.empty:
            row = df.iloc[0]
            net = float(row.get("net_amount", 0))
            if net > 0:
                return {"score": 65, "details": {"reason": f"主力资金净流入{net:.0f}万", "source": "tushare_api"}}
            elif net < 0:
                return {"score": 40, "details": {"reason": f"主力资金净流出{abs(net):.0f}万", "source": "tushare_api"}}
    except Exception as e:
        print(f"  ⚠️ {ts_code} Tushare moneyflow失败: {e}")

    # === 路径C: 用涨跌幅判断情绪（兜底） ===
    try:
        df = get_daily_price_db_first(ts_code, days_back=5)
        if df is not None and not df.empty and len(df) >= 5:
            recent = df["pct_chg"].iloc[:5].mean()
            if recent > 3:
                return {"score": 65, "details": {"reason": f"近5日涨幅{recent:.1f}%，情绪积极", "source": "price_fallback"}}
            elif recent < -3:
                return {"score": 35, "details": {"reason": f"近5日跌幅{recent:.1f}%，情绪悲观", "source": "price_fallback"}}
    except Exception as e:
        print(f"  ⚠️ {ts_code} 情绪评分价格数据获取失败: {e}")
    return {"score": 50, "details": {"reason": "无资金流数据，中性评分", "source": "neutral"}}


def score_rps(ts_code: str, trade_date: str = None) -> dict:
    """
    RPS相对价格强度因子评分（0-100）

    基于威廉·欧奈尔CAN SLIM体系：
    - RPS_120 ≥ 90 → 强势股特征（95分）
    - RPS_120 80-90 → 关注区（80分）
    - RPS_120 50-80 → 一般（60分）
    - RPS_120 30-50 → 偏弱（40分）
    - RPS_120 < 30 → 弱势回避（20分）
    - RPS_20 短期动量确认：若RPS_20 < 30则降级一档
    """
    if not _has_rps:
        return {"score": 50, "details": {"reason": "RPS模块不可用"}}

    try:
        rps = rps_engine.get_stock_rps(ts_code, trade_date, periods=[20, 60, 120])
        if not rps:
            return {"score": 50, "details": {"reason": "RPS数据不足"}}

        rps_120 = rps.get('rps_120')
        rps_20 = rps.get('rps_20')

        if rps_120 is None:
            return {"score": 50, "details": {"reason": "RPS_120无数据"}}

        # 基础评分
        if rps_120 >= 90:
            base_score = 95
            level = "极强"
        elif rps_120 >= 80:
            base_score = 80
            level = "偏强"
        elif rps_120 >= 50:
            base_score = 60
            level = "一般"
        elif rps_120 >= 30:
            base_score = 40
            level = "偏弱"
        else:
            base_score = 20
            level = "弱势"

        # RPS_20短期确认：RPS_20过低时降级
        if rps_20 is not None and rps_20 < 30 and base_score > 40:
            base_score -= 15
            level += "(短弱)"

        # RPS_20强势时加分
        if rps_20 is not None and rps_20 >= 90 and base_score < 95:
            base_score += 5
            level += "(短强)"

        final_score = max(0, min(100, base_score))

        detail = (f"RPS_120={rps_120:.0f}, RPS_60={rps.get('rps_60', 0):.0f}, "
                  f"RPS_20={rps_20:.0f} → {level}")

        return {"score": final_score, "details": {"reason": detail, "rps_120": rps_120, "rps_20": rps_20}}
    except Exception as e:
        print(f"  ⚠️ {ts_code} RPS评分失败: {e}")
        return {"score": 50, "details": {"reason": f"RPS评分异常: {e}"}}


_rps_cache_df = None
_rps_cache_date = None


def _enrich_scored_with_rps(scored: list, trade_date: str) -> list:
    """
    为已评分候选列表补充RPS因子（后处理，带缓存避免重复计算）

    在每个评分块 scored.sort()/筛选后调用一次。
    RPS权重10%，从其他因子等比例扣减。
    """
    global _rps_cache_df, _rps_cache_date

    if not scored or not _has_rps:
        return scored

    # 缓存RPS结果，避免每只股票重复计算全市场RPS
    if _rps_cache_date != trade_date or _rps_cache_df is None:
        _rps_cache_df = rps_engine.calc_all_rps(trade_date, periods=[20, 60, 120])
        _rps_cache_date = trade_date
        if _rps_cache_df is None or _rps_cache_df.empty:
            print("  [WARN] RPS缓存数据为空，跳过RPS因子")
            return scored

    rps_df = _rps_cache_df

    for s in scored:
        try:
            code = s['ts_code']
            row = rps_df[rps_df['ts_code'] == code]
            if row.empty:
                s.setdefault('factors', {})["RPS"] = 50
                s.setdefault('factor_details', {})["RPS"] = {"reason": "无RPS数据"}
            else:
                r = row.iloc[0]
                rps_120 = r.get('rps_120')
                rps_20 = r.get('rps_20')

                # 评分逻辑（与score_rps保持一致）
                if pd.notna(rps_120):
                    if rps_120 >= 90:
                        base_score = 95
                    elif rps_120 >= 80:
                        base_score = 80
                    elif rps_120 >= 50:
                        base_score = 60
                    elif rps_120 >= 30:
                        base_score = 40
                    else:
                        base_score = 20

                    if pd.notna(rps_20):
                        if rps_20 < 30 and base_score > 40:
                            base_score -= 15
                        elif rps_20 >= 90 and base_score < 95:
                            base_score += 5

                    rps_score = max(0, min(100, base_score))
                else:
                    rps_score = 50

                s.setdefault('factors', {})["RPS"] = rps_score
                s.setdefault('factor_details', {})["RPS"] = {
                    "reason": f"RPS_120={rps_120:.0f}, RPS_20={rps_20:.0f}" if pd.notna(rps_120) else "RPS数据不足"
                }

            # 更新强因子计数
            s['strong_factors'] = sum(1 for v in s['factors'].values() if isinstance(v, (int, float)) and v >= 60)
            # 10%权重注入总分
            original = s.get('total_score', 50)
            rps_val = s['factors'].get("RPS", 50)
            s['total_score'] = round(original * 0.9 + rps_val * 0.1, 1)
        except Exception:
            s.setdefault('factors', {})["RPS"] = 50
            s.setdefault('factor_details', {})["RPS"] = {"reason": "RPS后处理失败"}
    return scored


def score_growth(ts_code: str, trade_date: str) -> dict:
    """
    成长因子评分（0-100）— DB优先读取财务指标
    """
    global _db

    # 尝试从数据库读取
    fina_data = None
    if _db:
        try:
            fina_data = _db.get_fina_indicator(ts_code, latest=True)
        except Exception as e:
            print(f"  ⚠️ 读取{ts_code}财务数据失败: {e}")

    # 数据库命中
    if fina_data and fina_data.get("roe") is not None:
        score = 50
        details = {}

        rev_growth = fina_data.get("or_yoy")
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

        profit_growth = fina_data.get("profit_dedt_yoy")
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

        roe = fina_data.get("roe")
        if roe is not None:
            if roe > 15:
                score += 10
                details["ROE"] = f"{roe:.1f}%，优秀"
            elif roe > 8:
                score += 5
                details["ROE"] = f"{roe:.1f}%，良好"
            else:
                details["ROE"] = f"{roe:.1f}%，偏低"

        return {"score": max(0, min(100, score)), "details": details,
                "source": "db"}

    # 数据库未命中，回退到 Tushare API（并尝试写入DB）
    try:
        df = pro.fina_indicator(ts_code=ts_code, start_date=f"{trade_date[:4]}0101", end_date=trade_date)
        if df is not None and not df.empty:
            # 写入数据库
            try:
                if _db:
                    _db.upsert_fina_indicator(df)
            except Exception as e:
                print(f"  ⚠️ 缓存{ts_code}财务数据失败: {e}")

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

        rev_growth = row.get("or_yoy")
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

        profit_growth = row.get("profit_dedt_yoy")
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

        return {"score": max(0, min(100, score)), "details": details, "source": "api"}
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


def filter_held_stocks(scored: list, portfolio: dict) -> tuple:
    """过滤已持仓股票，返回(过滤后列表, 持仓冲突列表)"""
    holdings = portfolio.get("持仓列表", [])
    held_codes = {h.get("代码", "").split(".")[0] for h in holdings}
    filtered, conflicts = [], []
    for s in scored:
        code = s.get("ts_code", s.get("code", "")).split(".")[0]
        if code in held_codes:
            conflicts.append(s)
        else:
            filtered.append(s)
    if conflicts:
        print(f"  [WARN] 排除 {len(conflicts)} 只已持仓股票")
    return filtered, conflicts


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
    hot_sectors = get_hot_sectors(config["intelligence"], config)
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

    # 评分曲线配置（AlphaSift式非线性曲线）
    profile = _load_scoring_profile(config)

    # 模式权重（8因子体系，含流动性/稳定性/反转）
    weights = config.get("rules", {}).get("模式权重", {}).get("pre_market", {}).get("因子权重", {
        "估值": 12, "成长": 12, "动量": 20, "情绪": 15, "技术面": 12,
        "流动性": 10, "稳定性": 9, "反转": 10,
    })

    # 评分（早盘用昨日数据，8因子体系）
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
            liquidity = score_liquidity(ts_code, yesterday, profile)
            stability = score_stability(ts_code, yesterday, profile)
            reversal = score_reversal(ts_code, yesterday, profile)

            factor_scores = {"估值": valuation["score"], "成长": growth["score"],
                             "动量": momentum["score"], "技术面": technical["score"],
                             "情绪": sentiment["score"],
                             "流动性": liquidity["score"], "稳定性": stability["score"],
                             "反转": reversal["score"]}
            strong_factors = sum(1 for v in factor_scores.values() if v >= 60)

            total = (valuation["score"] * weights.get("估值", 12) + growth["score"] * weights.get("成长", 12)
                     + momentum["score"] * weights.get("动量", 20) + technical["score"] * weights.get("技术面", 12)
                     + sentiment["score"] * weights.get("情绪", 15)
                     + liquidity["score"] * weights.get("流动性", 10)
                     + stability["score"] * weights.get("稳定性", 9)
                     + reversal["score"] * weights.get("反转", 10)) / 100

            scored.append({
                "ts_code": ts_code,
                "total_score": round(total, 1),
                "strong_factors": strong_factors,
                "factors": factor_scores,
                "factor_details": {"估值": valuation["details"], "成长": growth["details"],
                                   "动量": momentum["details"], "技术面": technical["details"],
                                   "情绪": sentiment["details"],
                                   "流动性": liquidity["details"], "稳定性": stability["details"],
                                   "反转": reversal["details"]},
            })
            print(f"  {ok} {ts_code}: {total:.1f}分")
        except Exception as e:
            print(f"  {warn} {ts_code} 评分失败: {e}")

    # 补充RPS因子评分
    scored = _enrich_scored_with_rps(scored, today)
    # 排序取Top N（过滤已持仓股票）
    scored, held = filter_held_stocks(scored, config.get("portfolio", {}))
    result["held_excluded"] = held
    scored.sort(key=lambda x: x["total_score"], reverse=True)
    result["ranked_stocks"] = scored[:top_n]

    # 早盘附加检查
    for s in result["ranked_stocks"]:
        result.setdefault("pre_market_notes", []).append(
            f"{s['ts_code']}: RPS={s['factors'].get('RPS','N/A')} 动量{s['factors']['动量']} 技术{s['factors']['技术面']}"
        )

    print(f"\n  {'='*40}")
    print(f"  早盘候选 Top {top_n}")
    print(f"  {'='*40}")
    # 止损位计算（基于最新收盘价-7%）
    for s in result["ranked_stocks"]:
        try:
            df = pro.daily(ts_code=s["ts_code"])
            if df is not None and not df.empty and len(df) >= 20:
                df = df.sort_values("trade_date", ascending=False).reset_index(drop=True)
                close = df.iloc[0]["close"]
                s["current_price"] = round(float(close), 2)
                s["suggested_stop_loss"] = round(float(close) * _get_stop_loss_ratio(), 2)
            else:
                s["current_price"] = None
                s["suggested_stop_loss"] = None
        except Exception:
            s["current_price"] = None
            s["suggested_stop_loss"] = None

    for i, s in enumerate(result["ranked_stocks"], 1):
        f = s['factors']
        print(f"  {i}. {s['ts_code']} — {s['total_score']}分")
        print(f"     估{f['估值']} 成{f['成长']} 动{f['动量']} 技{f['技术面']} 情{f['情绪']} 流{f.get('流动性',0)} 稳{f.get('稳定性',0)} 反{f.get('反转',0)} 强:{s['strong_factors']}/8")

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
    except Exception:
        print(f"  {warn} 无法获取今日板块数据")

    # 2. 北向资金实时（盘中可获取）
    try:
        hsgt = pro.moneyflow_hsgt(start_date=today, end_date=today)
        if hsgt is not None and not hsgt.empty:
            last = hsgt.iloc[-1]
            result["capital_flow_summary"]["北向资金"] = {
                "net": float(last.get("north_money", last.get("north_net", 0))),
                "south_net": float(last.get("south_money", 0)),
            }
            net_val = result["capital_flow_summary"]["北向资金"]["net"]
            direction = "净流入" if net_val > 0 else "净流出"
            print(f"  {ok} 北向资金: {direction} {abs(net_val):.0f}万")
    except Exception:
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

    # 4. 盘中评分（侧重情绪/资金流，8因子体系）
    profile = _load_scoring_profile(config)
    weights = config.get("rules", {}).get("模式权重", {}).get("intraday", {}).get("因子权重", {
        "估值": 0, "成长": 5, "动量": 20, "情绪": 30, "技术面": 15,
        "流动性": 10, "稳定性": 10, "反转": 10,
    })

    scored = []
    for ts_code in candidates:
        if is_st_stock(ts_code):
            continue
        try:
            yesterday_ymd = (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")
            # 盘中模式跳过高权重因子（估值盘中无意义，D9-8）
            valuation = (score_valuation(ts_code, yesterday_ymd)
                         if weights.get("估值", 0) > 0 else {"score": 0, "details": {"reason": "估值=0，盘中不计算"}})
            growth = score_growth(ts_code, yesterday_ymd)
            momentum = score_momentum(ts_code)
            technical = score_technical(ts_code)
            sentiment = score_sentiment(ts_code, yesterday_ymd)
            liquidity = score_liquidity(ts_code, yesterday_ymd, profile)
            stability = score_stability(ts_code, yesterday_ymd, profile)
            reversal = score_reversal(ts_code, yesterday_ymd, profile)

            factor_scores = {"估值": valuation["score"], "成长": growth["score"],
                             "动量": momentum["score"], "技术面": technical["score"],
                             "情绪": sentiment["score"],
                             "流动性": liquidity["score"], "稳定性": stability["score"],
                             "反转": reversal["score"]}
            strong_factors = sum(1 for v in factor_scores.values() if v >= 60)

            total = (valuation["score"] * weights.get("估值", 0) + growth["score"] * weights.get("成长", 5)
                     + momentum["score"] * weights.get("动量", 20) + technical["score"] * weights.get("技术面", 15)
                     + sentiment["score"] * weights.get("情绪", 30)
                     + liquidity["score"] * weights.get("流动性", 10)
                     + stability["score"] * weights.get("稳定性", 10)
                     + reversal["score"] * weights.get("反转", 10)) / 100

            scored.append({
                "ts_code": ts_code,
                "total_score": round(total, 1),
                "strong_factors": strong_factors,
                "factors": factor_scores,
                "factor_details": {"估值": valuation["details"], "成长": growth["details"],
                                   "动量": momentum["details"], "技术面": technical["details"],
                                   "情绪": sentiment["details"],
                                   "流动性": liquidity["details"], "稳定性": stability["details"],
                                   "反转": reversal["details"]},
            })
        except Exception as e:
            print(f"  {warn} {ts_code} 评分失败: {e}")

    scored, held = filter_held_stocks(scored, config.get("portfolio", {}))
    result["held_excluded"] = result.get("held_excluded", []) + held
    scored = _enrich_scored_with_rps(scored, today)
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

    # 止损位计算
    for s in result["ranked_stocks"]:
        try:
            df = pro.daily(ts_code=s["ts_code"])
            if df is not None and not df.empty and len(df) >= 20:
                df = df.sort_values("trade_date", ascending=False).reset_index(drop=True)
                close = df.iloc[0]["close"]
                s["current_price"] = round(float(close), 2)
                s["suggested_stop_loss"] = round(float(close) * _get_stop_loss_ratio(), 2)
            else:
                s["current_price"] = None
                s["suggested_stop_loss"] = None
        except Exception:
            s["current_price"] = None
            s["suggested_stop_loss"] = None

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
    except Exception:
        print(f"  {warn} 板块数据暂不可用")

    # 资金流向
    try:
        hsgt = pro.moneyflow_hsgt(start_date=today, end_date=today)
        if hsgt is not None and not hsgt.empty:
            last = hsgt.iloc[-1]
            result["morning_summary"]["北向资金"] = round(float(last.get("north_money", last.get("north_net", 0))), 0)
    except Exception as e:
        print(f"  ⚠️ 北向资金数据获取失败: {e}")

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

    # 午盘权重（8因子体系，侧重情绪/资金流+动量）
    profile = _load_scoring_profile(config)
    weights = config.get("rules", {}).get("模式权重", {}).get("noon", {}).get("因子权重", {
        "估值": 12, "成长": 12, "动量": 15, "情绪": 20, "技术面": 12,
        "流动性": 10, "稳定性": 9, "反转": 10,
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
            liquidity = score_liquidity(ts_code, ymd, profile)
            stability = score_stability(ts_code, ymd, profile)
            reversal = score_reversal(ts_code, ymd, profile)

            factor_scores = {"估值": valuation["score"], "成长": growth["score"],
                             "动量": momentum["score"], "技术面": technical["score"],
                             "情绪": sentiment["score"],
                             "流动性": liquidity["score"], "稳定性": stability["score"],
                             "反转": reversal["score"]}
            strong_factors = sum(1 for v in factor_scores.values() if v >= 60)

            total = (valuation["score"] * weights.get("估值", 12) + growth["score"] * weights.get("成长", 12)
                     + momentum["score"] * weights.get("动量", 15) + technical["score"] * weights.get("技术面", 12)
                     + sentiment["score"] * weights.get("情绪", 20)
                     + liquidity["score"] * weights.get("流动性", 10)
                     + stability["score"] * weights.get("稳定性", 9)
                     + reversal["score"] * weights.get("反转", 10)) / 100

            scored.append({
                "ts_code": ts_code,
                "total_score": round(total, 1),
                "strong_factors": strong_factors,
                "factors": factor_scores,
                "factor_details": {"估值": valuation["details"], "成长": growth["details"],
                                   "动量": momentum["details"], "技术面": technical["details"],
                                   "情绪": sentiment["details"],
                                   "流动性": liquidity["details"], "稳定性": stability["details"],
                                   "反转": reversal["details"]},
            })
        except Exception as e:
            print(f"  {warn} {ts_code} 评分失败: {e}")

    scored, held = filter_held_stocks(scored, config.get("portfolio", {}))
    result["held_excluded"] = result.get("held_excluded", []) + held
    scored = _enrich_scored_with_rps(scored, today)
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
    # 止损位计算
    for s in result["ranked_stocks"]:
        try:
            df = pro.daily(ts_code=s["ts_code"])
            if df is not None and not df.empty and len(df) >= 20:
                df = df.sort_values("trade_date", ascending=False).reset_index(drop=True)
                close = df.iloc[0]["close"]
                s["current_price"] = round(float(close), 2)
                s["suggested_stop_loss"] = round(float(close) * _get_stop_loss_ratio(), 2)
            else:
                s["current_price"] = None
                s["suggested_stop_loss"] = None
        except Exception:
            s["current_price"] = None
            s["suggested_stop_loss"] = None

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
    hot_sectors = get_hot_sectors(config["intelligence"], config)
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

    # 完整评分（使用今日数据，8因子体系）
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")
    profile = _load_scoring_profile(config)
    weights = config.get("rules", {}).get("模式权重", {}).get("evening", {}).get("因子权重", {
        "估值": 18, "成长": 15, "动量": 12, "情绪": 10, "技术面": 12,
        "流动性": 12, "稳定性": 10, "反转": 11,
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
            liquidity = score_liquidity(ts_code, yesterday, profile)
            stability = score_stability(ts_code, yesterday, profile)
            reversal = score_reversal(ts_code, yesterday, profile)

            factor_scores = {"估值": valuation["score"], "成长": growth["score"],
                             "动量": momentum["score"], "技术面": technical["score"],
                             "情绪": sentiment["score"],
                             "流动性": liquidity["score"], "稳定性": stability["score"],
                             "反转": reversal["score"]}
            strong_factors = sum(1 for v in factor_scores.values() if v >= 60)

            total = (valuation["score"] * weights.get("估值", 18) + growth["score"] * weights.get("成长", 15)
                     + momentum["score"] * weights.get("动量", 12) + technical["score"] * weights.get("技术面", 12)
                     + sentiment["score"] * weights.get("情绪", 10)
                     + liquidity["score"] * weights.get("流动性", 12)
                     + stability["score"] * weights.get("稳定性", 10)
                     + reversal["score"] * weights.get("反转", 11)) / 100

            scored.append({
                "ts_code": ts_code,
                "total_score": round(total, 1),
                "strong_factors": strong_factors,
                "factors": factor_scores,
                "factor_details": {"估值": valuation["details"], "成长": growth["details"],
                                   "动量": momentum["details"], "技术面": technical["details"],
                                   "情绪": sentiment["details"],
                                   "流动性": liquidity["details"], "稳定性": stability["details"],
                                   "反转": reversal["details"]},
            })
        except Exception as e:
            print(f"  {warn} {ts_code} 评分失败: {e}")
            result["errors"].append(f"{ts_code} 评分异常: {e}")

    scored = _enrich_scored_with_rps(scored, today)
    scored, held = filter_held_stocks(scored, config.get("portfolio", {}))
    result["held_excluded"] = result.get("held_excluded", []) + held
    scored.sort(key=lambda x: x["total_score"], reverse=True)
    top_stocks = scored[:top_n]
    result["ranked_stocks"] = top_stocks

    # 行业集中度检查（按实际行业字段，非代码前缀）
    sector_counts = {}
    for s in scored:
        ind = get_stock_industry(s["ts_code"])
        s["industry"] = ind
        sector_counts[ind] = sector_counts.get(ind, 0) + 1
    total_stocks = max(len(scored), 1)
    for ind_name, count in sector_counts.items():
        pct = round(count / total_stocks * 100, 1)
        result["sector_concentration"][ind_name] = {"count": count, "pct": pct, "over_limit": pct > 40}
    if any(c["over_limit"] for c in result["sector_concentration"].values()):
        result["warnings"].append("行业集中度超40%限制")
        print(f"  {warn} 行业集中度超40%")

    # 止损位
    for s in top_stocks:
        try:
            df = pro.daily(ts_code=s["ts_code"])
            if df is not None and not df.empty and len(df) >= 20:
                # Tushare数据按trade_date升序，需降序取最新
                df = df.sort_values("trade_date", ascending=False).reset_index(drop=True)
                close = df.iloc[0]["close"]
                s["suggested_stop_loss"] = round(float(close) * _get_stop_loss_ratio(), 2)
                s["current_price"] = round(float(close), 2)
            else:
                s["suggested_stop_loss"] = None
                s["current_price"] = None
        except Exception:
            s["suggested_stop_loss"] = None
            s["current_price"] = None

    print(f"\n  {'='*40}")
    print(f"  晚间选股 Top {top_n}")
    print(f"  {'='*40}")
    for i, s in enumerate(top_stocks, 1):
        f = s['factors']
        print(f"  {i}. {s['ts_code']} — {s['total_score']}分 (强因子:{s.get('strong_factors', 0)}/8)")
        print(f"     估{f['估值']} 成{f['成长']} 动{f['动量']} 技{f['技术面']} 情{f['情绪']} 流{f.get('流动性',0)} 稳{f.get('稳定性',0)} 反{f.get('反转',0)}")

    if result.get("st_filtered"):
        print(f"\n  {warn} ST过滤: {len(result['st_filtered'])} 只")
    if result.get("review_deviation_notes"):
        print(f"\n  {ok} 已参考复盘偏差: {len(result['review_deviation_notes'])} 条")

    return result


# ============================================================
#  模式5: 全量扫描 (deep_scan) — 从全市场选股
# ============================================================

def get_stock_universe_from_db(today: str, max_candidates: int = 200) -> list:
    """
    从数据库批量获取全量股票候选池

    策略：
    1. 获取所有正常上市的股票（排除ST/退市）
    2. 通过SQL批量获取今日PE/换手率/涨跌幅
    3. 预筛选：PE合理、有成交、有财务数据
    4. 按换手率×|涨跌幅|排序取前max_candidates

    Returns:
        [{"ts_code": "000001.SZ", "name": "平安银行", ...}, ...]
    """
    global _db
    if not _db or not _db.conn:
        print("  [WARN] 数据库不可用，无法全量扫描")
        return []

    try:
        import pandas as pd

        # Step 1: 获取所有正常上市股票 + 排除ST/退市
        sql_base = """
            SELECT s.ts_code, s.name, s.industry, s.market,
                   COALESCE(d.pe_ttm, d.pe) as pe,
                   d.turnover_rate, d.total_mv, d.circ_mv,
                   p.pct_chg, p.amount, p.vol
            FROM stock_basic s
            JOIN daily_price p ON s.ts_code = p.ts_code AND p.trade_date = ?
            LEFT JOIN daily_basic d ON s.ts_code = d.ts_code AND d.trade_date = ?
            WHERE s.list_status = 'L'
              AND (s.name NOT LIKE '%ST%' AND s.name NOT LIKE '%退市%')
              AND p.pct_chg IS NOT NULL
        """
        df = pd.read_sql_query(sql_base, _db.conn, params=[today, today])

        if df.empty:
            print("  [WARN] 全量扫描无候选股票")
            return []

        print(f"  [OK] 全量基础数据: {len(df)} 只")

        # Step 2: 过滤无PE/无交易量的股票（打新债、ETF等）
        df = df[df['pe'].notna() & (df['pe'] > 0) & (df['pe'] < 200)].copy()
        print(f"  [OK] PE合理过滤后: {len(df)} 只")

        # Step 3: 过滤无成交金额的
        df = df[df['amount'].notna() & (df['amount'] > 0)].copy()

        # Step 4: 过滤换手率极低的僵尸股
        df = df[df['turnover_rate'].notna() & (df['turnover_rate'] > 0.3)].copy()
        print(f"  [OK] 换手率>0.3%过滤后: {len(df)} 只")

        # Step 5: 综合排序因子 = 换手率 * (1 + |涨跌幅|/10)
        df['score_heuristic'] = df['turnover_rate'] * (1 + df['pct_chg'].abs() / 10)
        df = df.sort_values('score_heuristic', ascending=False).head(max_candidates)

        candidates = df.to_dict('records')
        print(f"  [OK] 综合排序取前{len(candidates)}只进行多因子评分")

        return candidates

    except Exception as e:
        print(f"  [FAIL] 全量扫描失败: {e}")
        return []


def deep_scan_picks(top_n: int, config: dict, today: str) -> dict:
    """全量扫描选股 — 从全市场筛选"""
    ok, warn, fail = "[OK]", "[WARN]", "[FAIL]"
    print(f"[选股机器人] 模式=全量扫描 deep_scan top_n={top_n}...")

    result = {
        "mode": "deep_scan",
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "parameters": {"top_n": top_n},
        "scan_summary": {},
        "ranked_stocks": [],
        "st_filtered": [],
        "suspended_stocks": [],
        "sector_concentration": {},
        "review_deviation_notes": [],
        "warnings": [],
        "errors": [],
    }

    # 1. 从数据库获取全量候选
    db_candidates = get_stock_universe_from_db(today, max_candidates=200)
    result["scan_summary"]["total_candidates"] = len(db_candidates)

    if not db_candidates:
        result["errors"].append("全量扫描无候选，回退到晚间模式")
        print(f"  {fail} 候选为空，请检查数据库")
        return result

    # 2. 排除已持仓
    portfolio = config.get("portfolio", {})
    holdings = portfolio.get("持仓列表", [])
    held_codes = {h.get("代码", "").split(".")[0] for h in holdings}
    held_codes_full = set()
    for h in holdings:
        c = h.get("代码", "")
        if c:
            held_codes_full.add(c if "." in c else c)
            held_codes_full.add(c.split(".")[0] if "." in c else c)

    filtered = [c for c in db_candidates if c.get("ts_code", "").split(".")[0] not in held_codes]
    excluded_count = len(db_candidates) - len(filtered)
    if excluded_count:
        print(f"  {warn} 排除 {excluded_count} 只已持仓股票")
    result["scan_summary"]["held_excluded"] = excluded_count

    # 3. 停牌检查（批量：检查是否有today的行情数据）
    #    get_stock_universe_from_db 已经通过 daily_price JOIN 保证了有行情
    #    但有些可能今日停牌昨日有数据，再确认一下
    suspended = []
    active = []
    for c in filtered:
        if is_suspended(c["ts_code"], today):
            suspended.append(c["ts_code"])
        else:
            active.append(c)

    result["suspended_stocks"] = suspended
    result["scan_summary"]["suspended"] = len(suspended)

    if suspended:
        print(f"  {warn} 停牌 {len(suspended)} 只，跳过")

    if not active:
        result["errors"].append("无可用候选")
        return result

    # 4. 多因子评分（晚间权重，8因子体系）
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")
    profile = _load_scoring_profile(config)
    weights = config.get("rules", {}).get("模式权重", {}).get("evening", {}).get("因子权重", {
        "估值": 18, "成长": 15, "动量": 12, "情绪": 10, "技术面": 12,
        "流动性": 12, "稳定性": 10, "反转": 11,
    })

    scored = []
    total = len(active)
    for idx, c in enumerate(active):
        ts_code = c["ts_code"]
        if idx % 50 == 0:
            print(f"  [进度] 评分 {idx}/{total}...")

        try:
            valuation = score_valuation(ts_code, yesterday)
            growth = score_growth(ts_code, yesterday)
            momentum = score_momentum(ts_code)
            technical = score_technical(ts_code)
            sentiment = score_sentiment(ts_code, yesterday)
            liquidity = score_liquidity(ts_code, yesterday, profile)
            stability = score_stability(ts_code, yesterday, profile)
            reversal = score_reversal(ts_code, yesterday, profile)

            factor_scores = {"估值": valuation["score"], "成长": growth["score"],
                             "动量": momentum["score"], "技术面": technical["score"],
                             "情绪": sentiment["score"],
                             "流动性": liquidity["score"], "稳定性": stability["score"],
                             "反转": reversal["score"]}
            strong_factors = sum(1 for v in factor_scores.values() if v >= 60)

            total_score = (valuation["score"] * weights.get("估值", 18)
                          + growth["score"] * weights.get("成长", 15)
                          + momentum["score"] * weights.get("动量", 12)
                          + technical["score"] * weights.get("技术面", 12)
                          + sentiment["score"] * weights.get("情绪", 10)
                          + liquidity["score"] * weights.get("流动性", 12)
                          + stability["score"] * weights.get("稳定性", 10)
                          + reversal["score"] * weights.get("反转", 11)) / 100

            scored.append({
                "ts_code": ts_code,
                "name": c.get("name", ""),
                "total_score": round(total_score, 1),
                "strong_factors": strong_factors,
                "factors": factor_scores,
                "factor_details": {"估值": valuation["details"], "成长": growth["details"],
                                   "动量": momentum["details"], "技术面": technical["details"],
                                   "情绪": sentiment["details"],
                                   "流动性": liquidity["details"], "稳定性": stability["details"],
                                   "反转": reversal["details"]},
            })
        except Exception as e:
            result["errors"].append(f"{ts_code} 评分异常: {e}")

    # 5. 补充RPS因子 + 排序取Top N
    scored = _enrich_scored_with_rps(scored, today)
    scored.sort(key=lambda x: x["total_score"], reverse=True)
    top_stocks = scored[:top_n]
    result["ranked_stocks"] = top_stocks
    result["scan_summary"]["total_scored"] = len(scored)

    # 6. 行业集中度检查（按实际行业字段，非代码前缀）
    sector_counts = {}
    for s in scored:
        ind = get_stock_industry(s["ts_code"])
        s["industry"] = ind
        sector_counts[ind] = sector_counts.get(ind, 0) + 1
    total_s = max(len(scored), 1)
    for ind_name, count in sector_counts.items():
        pct = round(count / total_s * 100, 1)
        result["sector_concentration"][ind_name] = {"count": count, "pct": pct, "over_limit": pct > 40}
    if any(c["over_limit"] for c in result["sector_concentration"].values()):
        result["warnings"].append("行业集中度超40%限制")
        print(f"  {warn} 行业集中度超40%")

    # 7. 止损位
    for s in top_stocks:
        try:
            df = pro.daily(ts_code=s["ts_code"])
            if df is not None and not df.empty and len(df) >= 20:
                df = df.sort_values("trade_date", ascending=False).reset_index(drop=True)
                close = df.iloc[0]["close"]
                s["suggested_stop_loss"] = round(float(close) * _get_stop_loss_ratio(), 2)
                s["current_price"] = round(float(close), 2)
            else:
                s["suggested_stop_loss"] = None
                s["current_price"] = None
        except Exception:
            s["suggested_stop_loss"] = None
            s["current_price"] = None

    # 8. 打印结果
    print(f"\n  {'='*50}")
    print(f"  全量扫描选股 Top {top_n}")
    print(f"  {'='*50}")
    for i, s in enumerate(top_stocks, 1):
        name_str = f" ({s.get('name', '')})" if s.get('name') else ""
        f = s['factors']
        print(f"  {i}. {s['ts_code']}{name_str} — {s['total_score']}分 (强因子:{s.get('strong_factors', 0)}/8)")
        print(f"     估{f['估值']} 成{f['成长']} 动{f['动量']} 技{f['技术面']} 情{f['情绪']} 流{f.get('流动性',0)} 稳{f.get('稳定性',0)} 反{f.get('反转',0)}")

    return result


# ============================================================
#  主入口
# ============================================================

# ============================================================
#  L1→L2→L3 选股管线（借鉴AlphaSift三级筛选架构）
#
#  管线流程:
#    L1: 8因子全量评分 + 硬筛条件（在模式函数中完成）
#    L2: 重排序（可选LLM/规则混合，当前为规则启发式）
#    L3: 后置分析器（Scorecard + Risk Overlay）
# ============================================================

# __pipeline_cache: L3各模块的延迟导入缓存
__pipeline_cache = {}


def _get_scorecard():
    """延迟导入 Scorecard"""
    if "scorecard" not in __pipeline_cache:
        from scripts.utils.scorecard import Scorecard
        __pipeline_cache["scorecard"] = Scorecard()
    return __pipeline_cache["scorecard"]


def _get_risk_overlay(profile: dict = None):
    """延迟导入 RiskOverlay"""
    key = f"risk_overlay_{id(profile) if profile else 'default'}"
    if key not in __pipeline_cache:
        from scripts.utils.risk_overlay import RiskOverlay
        __pipeline_cache[key] = RiskOverlay(profile=profile)
    return __pipeline_cache[key]


def _l2_rerank(scored: list, trade_date: str = None) -> list:
    """
    L2 层：重排序（无需LLM的规则启发式）

    当前实现基于强因子数量和稳定性做二次加权排序。
    未来可升级为LLM相对排序（当配置了LLM API后）。

    规则：
    - 基础分 = total_score * 0.8
    - 强因子(>=60)加分：每多一个强因子+5分（最多+20分）
    - 稳定性惩罚：稳定性<40分时，总分额外扣10%（不稳定的票降级）
    """
    if not scored:
        return scored

    for s in scored:
        factors = s.get("factors", {})
        stability = factors.get("稳定性", 50)
        strong_count = s.get("strong_factors", 0)

        # 强因子加分
        bonus = min(20, strong_count * 5)

        # 稳定性惩罚
        if stability < 40:
            stability_penalty = s["total_score"] * 0.1
        else:
            stability_penalty = 0

        # L2 调整分（保留原分作为参考）
        s["_l2_adjustment"] = round(bonus - stability_penalty, 1)
        s["total_score_l2"] = round(s["total_score"] + s["_l2_adjustment"], 1)

    # 按L2调整后排
    scored.sort(key=lambda x: x.get("total_score_l2", x["total_score"]), reverse=True)
    return scored


def _l3_post_analysis(ranked_stocks: list, today: str, profile: dict = None,
                      mode: str = "evening") -> list:
    """
    L3 层：后置分析器（Scorecard + Risk Overlay）

    对 Top N 候选进行最终审核：
    1. Scorecard：本地规则加减分（突破/量价/均线/板块/基本面）
    2. Risk Overlay：独立风险惩罚/否决

    Args:
        ranked_stocks: L2排序后的候选列表
        today: 交易日YYYYMMDD
        profile: 评分曲线配置
        mode: 选股模式

    Returns:
        增强后的候选列表（增加scorecard/risk_overlay字段）
    """
    if not ranked_stocks:
        return ranked_stocks

    ok, warn = "[OK]", "[WARN]"
    print(f"\n  {'='*40}")
    print(f"  L3 后置分析器: Scorecard + Risk Overlay")
    print(f"  {'='*40}")

    # L3a: Scorecard 评估
    scorecard = _get_scorecard()
    card_results = scorecard.evaluate_batch(ranked_stocks, today)

    # L3b: Risk Overlay 风险叠加
    risk_overlay = _get_risk_overlay(profile)

    enhanced = []
    for i, s in enumerate(ranked_stocks):
        ts_code = s["ts_code"]

        # 合并Scorecard结果
        card = card_results[i] if i < len(card_results) else {"delta": 0, "reasons": [], "tags": [], "confidence": "低"}

        # Risk Overlay
        risk_result = risk_overlay.apply_to_stock(ts_code, today, s["total_score"])

        # 否决处理
        if risk_result["veto"]:
            s["_vetoed"] = True
            s["_veto_reasons"] = risk_result["reasons"]
            print(f"  {warn} {ts_code} 被风险叠加层否决: {'; '.join(risk_result['reasons'][:2])}")
            continue

        # 最终分数 = L1分数 + Scorecard调整 - Risk惩罚
        scorecard_delta = card.get("delta", 0)
        risk_penalty = risk_result.get("penalty_applied", 0)

        # 确保L3分数不超过100
        final_score = max(0, min(100, s["total_score"] + scorecard_delta - risk_penalty))

        s["_scorecard"] = {
            "delta": scorecard_delta,
            "reasons": card.get("reasons", []),
            "tags": card.get("tags", []),
            "confidence": card.get("confidence", "低"),
        }
        s["_risk_overlay"] = {
            "penalty": risk_penalty,
            "veto": False,
            "reasons": risk_result.get("reasons", []),
        }
        s["total_score_l3"] = round(final_score, 1)

        # 更新打印：列出scorecard加分和风险惩罚
        print(f"  {ok} {ts_code}: L1={s['total_score']} L3={final_score} (card={scorecard_delta:+.0f} risk=-{risk_penalty:.0f})")

        enhanced.append(s)

    # 按L3最终分排序
    enhanced.sort(key=lambda x: x.get("total_score_l3", x["total_score"]), reverse=True)

    print(f"  L3完成: {len(enhanced)}/{len(ranked_stocks)} 通过风险审核")
    return enhanced


def generate_stock_picks(top_n: int = 5, mode: str = "evening", deep_scan: bool = False) -> dict:
    """
    主函数：L1→L2→L3 选股管线

    管线流程：
    1. L1: 8因子全量评分（按模式分发到具体函数）
    2. L2: 规则启发式重排序
    3. L3: Scorecard + Risk Overlay 后置分析
    """
    root = os.path.join(os.path.dirname(__file__), "..", "..")
    config = load_config(root)
    today = datetime.now().strftime("%Y%m%d")
    profile = _load_scoring_profile(config)

    mode_map = {
        "pre_market": pre_market_picks,
        "intraday": intraday_picks,
        "noon": noon_picks,
        "evening": evening_picks,
    }

    if deep_scan:
        func = deep_scan_picks
    else:
        func = mode_map.get(mode, evening_picks)

    # === L1: 因子评分（模式函数） ===
    result = func(top_n, config, today)

    # 统一标记
    result["mode"] = mode
    if deep_scan:
        result["mode"] = "deep_scan"

    # 获取评分过的候选
    ranked = result.get("ranked_stocks", [])

    if not ranked:
        print("  [WARN] 无候选股票，跳过L2/L3管线")
        result["pipeline"] = {"L1": "completed", "L2": "skipped", "L3": "skipped"}
        return result

    # === L2: LLM相对排序 / 规则启发式（自动降级） ===
    try:
        from scripts.utils.l2_rerank import rank_with_llm_or_fallback as l2_rank_fn
        from scripts.utils.l2_rerank import is_llm_available
        use_llm_l2 = True
        l2_mode = "LLM相对排序" if is_llm_available() else "规则启发式(LLM未配置)"
    except Exception:
        l2_rank_fn = _l2_rerank
        use_llm_l2 = False
        l2_mode = "内联规则启发式"

    print(f"\n  {'='*40}")
    print(f"  L2: {l2_mode}")
    print(f"  {'='*40}")

    if use_llm_l2:
        try:
            ranked = l2_rank_fn(ranked, today, mode=mode)
        except Exception as e:
            print(f"  [WARN] L2 LLM排序失败: {e}，降级到规则排序")
            ranked = _l2_rerank(ranked, today)
    else:
        ranked = l2_rank_fn(ranked, today)
    result["ranked_stocks"] = ranked[:top_n]  # L2后重新截断
    result["_l2_applied"] = True

    # === L3: 后置分析器 ===
    l3_enhanced = _l3_post_analysis(ranked[:top_n], today, profile, mode=mode)
    result["ranked_stocks"] = l3_enhanced

    # 记录被否决的候选
    vetoed = [s for s in ranked if s.get("_vetoed")]
    if vetoed:
        result["_vetoed_stocks"] = vetoed

    result["pipeline"] = {
        "L1": "completed",
        "L2": "completed",
        "L3": "completed",
        "final_count": len(l3_enhanced),
        "vetoed_count": len(vetoed),
    }

    # L3后打印最终排名
    if l3_enhanced:
        print(f"\n  {'='*40}")
        print(f"  L3 最终排名 Top {min(top_n, len(l3_enhanced))}")
        print(f"  {'='*40}")
        for i, s in enumerate(l3_enhanced, 1):
            f = s['factors']
            l3 = s.get("total_score_l3", s["total_score"])
            sc_tags = s.get("_scorecard", {}).get("tags", [])
            tag_str = f" [{','.join(sc_tags)}]" if sc_tags else ""
            print(f"  {i}. {s['ts_code']} — L3={l3} (L1={s['total_score']}){tag_str}")
        if vetoed:
            print(f"\n  ⛔ 风险否决: {len(vetoed)} 只")

    return result


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="多因子选股 (4种模式 + 全量扫描)")
    parser.add_argument("--top-n", type=int, default=5, help="输出候选数量")
    parser.add_argument("--force-refresh", action="store_true", help="强制刷新数据")
    parser.add_argument("--mode", type=str, default="evening",
                        choices=["pre_market", "intraday", "noon", "evening"],
                        help="选股模式: pre_market(早盘)/intraday(盘中)/noon(午盘)/evening(晚间)")
    parser.add_argument("--deep-scan", action="store_true", default=False,
                        help="全量扫描模式：从全市场选股（默认false，仅热点板块）")
    args = parser.parse_args()

    print(f"\n{'='*50}")
    print(f"  选股机器人 — 模式: {args.mode}{'(全量扫描)' if args.deep_scan else ''}")
    print(f"{'='*50}\n")

    report = generate_stock_picks(args.top_n, args.mode, deep_scan=args.deep_scan)

    print("\n=== RESULT_JSON ===")
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

    # 写入报告日志（尽力而为）
    try:
        if _db:
            status = "ok" if not report.get("errors") else "error"
            _db.save_report_log(today, "agent5", status,
                               error_msg="; ".join(report["errors"]) if report.get("errors") else None)
    except Exception as e:
        print(f"  ⚠️ 保存报告日志失败: {e}")
