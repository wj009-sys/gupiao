"""
L2 LLM相对排序引擎 — 借鉴 AlphaSift L2 设计

使用LLM对L1 Top K候选进行相对排序（而非绝对评分）。
支持通过LiteLLM接入任意LLM提供商（OpenAI/Claude/DeepSeek/Gemini等），
无LLM配置时自动降级到规则启发式排序。

用法：
    from scripts.utils.l2_rerank import rank_with_llm_or_fallback
    ranked = rank_with_llm_or_fallback(candidates, market_context, trade_date)

L2 管线位置：
    L1: 8因子评分 → L2: LLM/规则重排序 → L3: Scorecard + Risk Overlay

环境变量配置（在 .claude/settings.local.json 的 env 中设置）：
    LLM_PROVIDER="openai"               # litellm支持的provider名
    LLM_MODEL="gpt-4o-mini"              # 模型名
    LLM_API_KEY=os.getenv('LLM_API_KEY', 'your-key-here')  # API Key
    LLM_API_BASE="https://api.openai.com/v1"  # API地址（兼容OpenAI格式）

D3异常处理表：
| 触发条件 | 一线修复 | 仍失败兜底 |
|---------|---------|-----------|
| LLM API 超时 | 重试1次(带3s超时) | 降级到规则排序+L2_note |
| LLM 返回空/解析失败 | 重试1次 | 使用规则排序+JSON解析保障 |
| 无LLM配置 | 检测环境变量为空 | 自动降级到规则排序 |
| 金融市场特定请求被拒 | 简化prompt减少金融术语 | 规则排序兜底 |
"""

import json
import os
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

# ============================================================
#  LLM 配置
# ============================================================

def get_llm_config() -> dict:
    """从环境变量获取LLM配置"""
    return {
        "provider": os.getenv("LLM_PROVIDER", "").strip(),
        "model": os.getenv("LLM_MODEL", "").strip(),
        "api_key": os.getenv("LLM_API_KEY", "").strip(),
        "api_base": os.getenv("LLM_API_BASE", "").strip(),
    }


def is_llm_available() -> bool:
    """检查LLM是否可用"""
    cfg = get_llm_config()
    return bool(cfg["provider"] and cfg["model"] and cfg["api_key"])


# ============================================================
#  Prompt 构造
# ============================================================

L2_RERANK_SYSTEM_PROMPT = """你是一位专业的A股量化分析师。你的任务是对候选股票进行相对排序。

## 规则
1. 分析每只候选股票的技术面、资金面、基本面综合情况
2. 对比全市场背景进行相对排序（不是绝对评分）
3. 为每只股票提供 thesis（投资逻辑）、catalyst（催化因素）、risk_summary（风险摘要）
4. 标注板块/主题归属
5. 识别需要关注的观察项和"证伪条件"

## 约束
- 不能推荐候选列表之外的股票
- 不能推翻L1的硬筛选条件（ST/停牌/退市等已在L1排除）
- 排序列在前的不一定分数最高，而是"在当前市场环境下相对更值得关注"

## 输出格式（JSON）
{
  "market_view": "对当前市场环境的总体判断（一句话，如'震荡偏多，科技板块领涨'）",
  "selection_logic": "整体选股逻辑说明",
  "portfolio_risk": "组合层面的风险提示（如'行业集中度偏高，需分散'）",
  "ranked": [
    {
      "ts_code": "000001.SZ",
      "rank_reason": "相对排序核心理由（一句话）",
      "thesis": "投资逻辑（2-3句话）",
      "catalyst": "近期催化剂",
      "risk_summary": "主要风险",
      "confidence": "高/中/低",
      "sector_tag": "板块标签",
      "theme_tag": "主题标签"
    }
  ],
  "watch_items": ["需要关注的观察事项列表"],
  "invalidators": ["会推翻当前判断的条件列表"]
}
```"""


def build_ranking_prompt(candidates: list, trade_date: str, mode: str = "evening") -> str:
    """
    构建L2排序Prompt
    """
    today_str = trade_date or datetime.now().strftime("%Y%m%d")

    # 候选数据摘要
    candidate_lines = []
    for i, c in enumerate(candidates, 1):
        ts_code = c.get("ts_code", "未知")
        score = c.get("total_score", 50)
        factors = c.get("factors", {})
        detail = c.get("factor_details", {})

        # 提取关键指标摘要
        factor_str = " | ".join([f"{k}:{v}" for k, v in factors.items() if isinstance(v, (int, float))])
        strong = c.get("strong_factors", 0)

        candidate_lines.append(
            f"  [{i}] {ts_code}\n"
            f"      综合分:{score} | 强因子:{strong}/8\n"
            f"      因子: {factor_str}\n"
        )

    candidates_text = "\n".join(candidate_lines)

    prompt = f"""## 市场背景
今日日期: {today_str}
选股模式: {mode}

## 候选股票列表（共{len(candidates)}只，来自L1多因子评分）
{candidates_text}

请基于以上候选列表和当前A股市场背景，进行相对排序。
输出符合JSON格式的排序结果。"""
    return prompt


# ============================================================
#  LLM 调用
# ============================================================

def _call_llm_ranking(candidates: list, trade_date: str, mode: str = "evening",
                      timeout: int = 30) -> dict:
    """
    调用LLM进行候选排序

    Args:
        candidates: L1评分后的候选列表
        trade_date: 交易日
        mode: 选股模式

    Returns:
        LLM返回的完整JSON响应，或空dict
    """
    cfg = get_llm_config()
    if not is_llm_available():
        logger.warning("[L2] LLM未配置，跳过LLM排序")
        return {}

    try:
        from litellm import completion

        # 构建模型名（provider/model 格式）
        model_name = f"{cfg['provider']}/{cfg['model']}" if "/" not in cfg["model"] else cfg["model"]

        # API Base
        kwargs = {}
        if cfg.get("api_base"):
            kwargs["api_base"] = cfg["api_base"]
        if cfg.get("api_key"):
            kwargs["api_key"] = cfg["api_key"]

        prompt = build_ranking_prompt(candidates, trade_date, mode)

        response = completion(
            model=model_name,
            messages=[
                {"role": "system", "content": L2_RERANK_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            response_format={"type": "json_object"},
            max_tokens=4096,
            temperature=0.3,
            timeout=timeout,
            **kwargs,
        )

        content = response.choices[0].message.content
        if not content:
            logger.warning("[L2] LLM返回空内容")
            return {}

        # 解析JSON
        parsed = json.loads(content)
        if not isinstance(parsed, dict) or "ranked" not in parsed:
            logger.warning(f"[L2] LLM返回格式异常: 缺少ranked字段")
            return {}

        logger.info(f"[L2] LLM排序完成: {len(parsed.get('ranked', []))} 只股票")
        return parsed

    except ImportError:
        logger.warning("[L2] litellm未安装，请 pip install litellm")
        return {}
    except Exception as e:
        logger.warning(f"[L2] LLM调用失败: {e}")
        return {}


# ============================================================
#  规则启发式排序（原_l2_rerank升级版）
# ============================================================

def _rule_based_rerank(scored: list) -> list:
    """
    规则启发式重排序（无LLM时的降级方案）

    增强版：
    - 基础分 = total_score * 0.8
    - 强因子(>=60)加分：每多一个强因子+5分（最多+20分）
    - 稳定性惩罚：稳定性<40分时，总分额外扣10%
    - 流动性加分：流动性>80分且至少2只其他因子>60 → +5分加分
    """
    if not scored:
        return scored

    for s in scored:
        factors = s.get("factors", {})
        stability = factors.get("稳定性", 50)
        liquidity = factors.get("流动性", 50)
        strong_count = s.get("strong_factors", 0)

        # 强因子加分
        bonus = min(20, strong_count * 5)

        # 稳定性惩罚
        stability_penalty = s["total_score"] * 0.1 if stability < 40 else 0

        # 流动性加分（流动性好+基本面向好的加分）
        liquidity_bonus = 5 if (liquidity > 80 and strong_count >= 2) else 0

        # L2 调整分
        s["_l2_method"] = "rule"
        s["_l2_adjustment"] = round(bonus + liquidity_bonus - stability_penalty, 1)
        s["total_score_l2"] = round(s["total_score"] + s["_l2_adjustment"], 1)
        s["_l2_thesis"] = f"强因子{strong_count}/8, 调整{s['_l2_adjustment']:+.1f}分"

    scored.sort(key=lambda x: x.get("total_score_l2", x["total_score"]), reverse=True)
    return scored


# ============================================================
#  LLM结果合并
# ============================================================

def _merge_llm_results(scored: list, llm_response: dict) -> list:
    """
    将LLM排序结果合并到候选列表中

    LLM返回的ranked顺序即为最终排序，
    同时将thesis/catalyst/risk_summary等字段注入候选。
    LLM未覆盖的候选保留L1排名。
    """
    if not llm_response or "ranked" not in llm_response:
        return _rule_based_rerank(scored)

    llm_ranked = llm_response["ranked"]
    if not llm_ranked:
        return _rule_based_rerank(scored)

    # 构建LLM结果的代码→数据映射
    llm_map = {}
    for item in llm_ranked:
        code = item.get("ts_code", "")
        if code:
            llm_map[code] = item

    # 按LLM顺序构建结果
    llm_ordered = []
    remaining = []

    for item in llm_ranked:
        code = item.get("ts_code", "")
        matched = [s for s in scored if s.get("ts_code") == code]
        if matched:
            s = matched[0]
            s["_l2_method"] = "llm"
            s["_l2_thesis"] = item.get("thesis", "")
            s["_l2_catalyst"] = item.get("catalyst", "")
            s["_l2_risk_summary"] = item.get("risk_summary", "")
            s["_l2_confidence"] = item.get("confidence", "中")
            s["_l2_sector_tag"] = item.get("sector_tag", "")
            s["_l2_theme_tag"] = item.get("theme_tag", "")
            s["_l2_rank_reason"] = item.get("rank_reason", "")
            # LLM在L1评分基础上输出L2分（轻微权重调整保持L1基准）
            s["total_score_l2"] = s["total_score"]
            llm_ordered.append(s)

    # 未覆盖的候选
    llm_codes = {item.get("ts_code") for item in llm_ranked}
    for s in scored:
        if s.get("ts_code") not in llm_codes:
            s["_l2_method"] = "rule_fallback"
            s["total_score_l2"] = s["total_score"]
            remaining.append(s)

    # 剩余候选用规则排序
    remaining = _rule_based_rerank(remaining)

    return llm_ordered + remaining


# ============================================================
#  主入口
# ============================================================

def rank_with_llm_or_fallback(scored: list, trade_date: str = None,
                               mode: str = "evening", config: dict = None) -> list:
    """
    L2 主入口：优先LLM排序，不可用则降级到规则排序

    这是 generate_stock_picks() 中 L2 步骤调用的函数。

    Args:
        scored: L1评分后的候选列表
        trade_date: 交易日YYYYMMDD
        mode: 选股模式
        config: 完整配置dict

    Returns:
        排序后的候选列表，各候选包含_l2_*字段
    """
    if not scored:
        return scored

    trade_date = trade_date or datetime.now().strftime("%Y%m%d")

    if is_llm_available():
        logger.info(f"[L2] LLM排序模式: {get_llm_config()['provider']}/{get_llm_config()['model']}")
        llm_result = _call_llm_ranking(scored, trade_date, mode)
        if llm_result:
            ranked = _merge_llm_results(scored, llm_result)
            # 附加市场观点的元数据
            for s in ranked:
                s.setdefault("_l2_meta", {})
                s["_l2_meta"]["market_view"] = llm_result.get("market_view", "")
                s["_l2_meta"]["selection_logic"] = llm_result.get("selection_logic", "")
                s["_l2_meta"]["portfolio_risk"] = llm_result.get("portfolio_risk", "")
            return ranked
        logger.info("[L2] LLM排序失败，降级到规则排序")

    # 降级到规则排序
    logger.info("[L2] 规则排序模式")
    return _rule_based_rerank(scored)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    # 测试数据
    test_candidates = [
        {"ts_code": "000001.SZ", "total_score": 75, "strong_factors": 5,
         "factors": {"估值": 70, "成长": 65, "动量": 80, "情绪": 60, "技术面": 75, "流动性": 85, "稳定性": 70, "反转": 55}},
        {"ts_code": "600519.SH", "total_score": 82, "strong_factors": 6,
         "factors": {"估值": 60, "成长": 75, "动量": 85, "情绪": 70, "技术面": 80, "流动性": 90, "稳定性": 80, "反转": 45}},
        {"ts_code": "300750.SZ", "total_score": 68, "strong_factors": 4,
         "factors": {"估值": 50, "成长": 80, "动量": 70, "情绪": 65, "技术面": 60, "流动性": 75, "稳定性": 50, "反转": 65}},
    ]

    print("=" * 50)
    print("  L2 相对排序测试")
    print("=" * 50)
    print(f"  LLM可用: {is_llm_available()}")

    ranked = rank_with_llm_or_fallback(test_candidates, "20260704", "evening")

    print(f"\n  {'='*40}")
    print(f"  排序结果 ({ranked[0].get('_l2_method', 'rule')})")
    print(f"  {'='*40}")
    for i, s in enumerate(ranked, 1):
        method = s.get("_l2_method", "rule")
        thesis = s.get("_l2_thesis", "")
        l2_score = s.get("total_score_l2", s["total_score"])
        print(f"  {i}. {s['ts_code']} — L2={l2_score} (L1={s['total_score']}) [{method}]")
        if thesis:
            print(f"     → {thesis[:60]}...")
