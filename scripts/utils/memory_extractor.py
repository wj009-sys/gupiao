"""
Memory Extractor — 从 Agent 报告自动提取知识，沉淀到知识库

基于 learn-claude-code s09 Memory 的提取模式。
每次 Daily Pipeline 完成后运行，实现 Karpathy LLM Wiki 范式：
"分析结论、策略调整、新发现的模式，必须写入 knowledge/ 对应文件"

提取流程：
  1. 收集指定日期所有 Agent 报告
  2. 对每份报告，提取结构化信息（规则 + 可选 LLM 增强）
  3. 去重（与已有 knowledge 对比）
  4. 写入 knowledge/ 对应文件
  5. 更新 INDEX.md 和 CHANGES.md

提取的知识类型：
  - market_observation:  市场关键观察和模式
  - strategy_adjustment: 策略调整建议
  - risk_discovery:      风控新发现
  - pattern_change:      模式/因子表现变化
  - rule_update:         规则更新建议
  - performance_note:    策略表现反馈

用法：
    from scripts.utils.memory_extractor import MemoryExtractor

    extractor = MemoryExtractor()
    result = extractor.extract("20260707")
    print(result.summary())

    # 带 LLM 增强提取
    from scripts.utils.llm_client import LLMClient
    extractor = MemoryExtractor(llm_client=LLMClient())
    result = extractor.extract("20260707")

D3异常处理表：
| 触发条件 | 一线修复 | 仍失败兜底 |
|---------|---------|-----------|
| 报告文件不存在（非交易日）| 往前找最近日期 | 返回空提取结果 |
| 报告文件编码异常 | utf-8→utf-8-sig→gbk 逐级回退 | 跳过该报告 |
| LLM 提取失败 | 规则提取降级 | 返回规则提取结果 |
| 知识库文件写入失败 | 检查目录权限 | 仅打印警告 |
| CHANGES.md 更新失败 | 跳过此步 | 文件保护：不覆盖已有内容 |
| 重复知识检测 | 按 name/description 模糊匹配 | 保留两者，标注 related |

D4 CHECKPOINT:
- CP1-报告存在性：collect_reports 后检查至少 1 份报告
- CP2-提取结果非空：extract 后检查 findings > 0
- CP3-去重：写入前检查 knowledge/ 已有文件
- CP4-格式校验：提取的知识必须包含 type/name/content
- CP5-知识库健康度：提取完成后建议运行 knowledge_lint

D9反例：
- 不要提取已存在的知识（必须有去重检查）
- 不要把原始报告照搬到知识库（必须提炼精华）
- 不要忽略 Agent 间的信号矛盾（标注 disputed）
- 不要在生产环境只依赖 LLM 提取（必须有规则降级）
"""
import os
import re
import json
import glob
import logging
from datetime import datetime, date as date_type
from typing import Optional

logger = logging.getLogger(__name__)

# ===== 项目根 =====

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


# ===== 知识类型定义 =====

EXTRACTION_TYPES = {
    "market_observation": "市场关键观察和模式",
    "strategy_adjustment": "策略调整建议",
    "risk_discovery": "风控新发现",
    "pattern_change": "模式/因子表现变化",
    "rule_update": "规则更新建议",
    "performance_note": "策略表现反馈",
}

# Agent 报告路径模板（YYYYMMDD 和 YYYY-MM-DD 两种格式）
REPORT_PATTERNS = [
    # (agent_id, glob_pattern)
    ("agent1", "reports/日报/情报/情报摘要_{date}*.md"),
    ("agent2", "reports/日报/分析/分析报告_{date}*.md"),
    ("agent3", "reports/日报/风控/*{date}*.md"),
    ("agent5", "reports/日报/选股/选股建议_{date}*.md"),
    ("agent6", "reports/日报/操盘/交易计划_{date}*.md"),
    ("agent7", "reports/日报/决策/投资决策_{date}*.md"),
    ("agent8", "reports/日报/政策/政策分析_{date}*.md"),
    ("agent9", "reports/日报/游资/游资追踪_{date}*.md"),
]

# 知识库目标文件
KNOWLEDGE_STRATEGY_DIR = os.path.join(PROJECT_ROOT, "knowledge", "策略")
KNOWLEDGE_REVIEW_DIR = os.path.join(PROJECT_ROOT, "knowledge", "复盘记录")
KNOWLEDGE_INDEX_PATH = os.path.join(PROJECT_ROOT, "knowledge", "INDEX.md")
KNOWLEDGE_CHANGES_PATH = os.path.join(PROJECT_ROOT, "knowledge", "CHANGES.md")

# 提取文件名前缀
EXTRACT_FILENAME_PREFIX = "提取_"


# ===== 日期工具 =====

def _normalize_date(date_str: str) -> str:
    """将日期统一为 YYYY-MM-DD 格式"""
    d = date_str.replace("-", "")
    if len(d) == 8 and d.isdigit():
        return f"{d[:4]}-{d[4:6]}-{d[6:8]}"
    return date_str


def _compact_date(date_str: str) -> str:
    """将日期转为 YYYYMMDD 格式"""
    return date_str.replace("-", "")


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


# ===== 提取结果 =====

class KnowledgeItem:
    """一条提取出的知识"""

    def __init__(self, item_type: str, name: str, content: str,
                 source_agent: str = "", source_report: str = "",
                 confidence: str = "medium"):
        assert item_type in EXTRACTION_TYPES, f"未知知识类型: {item_type}"
        self.type = item_type
        self.name = name
        self.content = content
        self.source_agent = source_agent
        self.source_report = source_report
        self.confidence = confidence  # high / medium / low
        self.tags: list[str] = []

    def to_dict(self) -> dict:
        return {
            "type": self.type,
            "name": self.name,
            "content": self.content[:200],
            "source": self.source_agent,
            "confidence": self.confidence,
            "tags": self.tags,
        }

    def to_markdown(self) -> str:
        return (
            f"### {self.name}\n"
            f"- **类型**: {EXTRACTION_TYPES.get(self.type, self.type)}\n"
            f"- **来源**: {self.source_agent}\n"
            f"- **置信度**: {self.confidence}\n"
            f"- **标签**: {', '.join(self.tags) if self.tags else '无'}\n\n"
            f"{self.content}\n"
        )


class ExtractionResult:
    """一次提取操作的结果"""

    def __init__(self, date: str):
        self.date = date
        self.items: list[KnowledgeItem] = []
        self.reports_loaded: int = 0
        self.reports_failed: int = 0
        self.llm_used: bool = False
        self.errors: list[str] = []

    def add(self, item: KnowledgeItem):
        self.items.append(item)

    def summary(self) -> str:
        types = {}
        for item in self.items:
            types[item.type] = types.get(item.type, 0) + 1
        type_summary = " | ".join(f"{EXTRACTION_TYPES.get(k, k)}: {v}" for k, v in sorted(types.items()))
        return (
            f"📋 知识提取 — {self.date}\n"
            f"   报告: {self.reports_loaded} 份 | 发现: {len(self.items)} 条\n"
            f"   LLM: {'✅' if self.llm_used else '❌'} | "
            f"   类型: {type_summary}\n"
        )

    def to_dict(self) -> dict:
        return {
            "date": self.date,
            "reports_loaded": self.reports_loaded,
            "reports_failed": self.reports_failed,
            "items": [i.to_dict() for i in self.items],
            "llm_used": self.llm_used,
            "errors": self.errors,
        }


# ===== 规则提取器 =====

class RuleExtractor:
    """基于规则的提取器（无需 LLM，保证基本提取能力）

    配合 Agent 报告的真实结构设计。三类提取：
      1. 结构匹配：表格行 (`| xxx | yyy | zzz |`) → 提取关键字段
      2. 关键词匹配：关键信号词+数值 → 提取精华
      3. 置信度自动定级：有数字+代码→high, 有数字→medium, 纯文本→low
    """

    # ── 股票代码提取（行内） ──
    STOCK_CODE_RE = re.compile(r"\b(\d{6})\.(SH|SZ)\b")
    STOCK_NAME_IN_BOLD = re.compile(r"\*\*([^*]+?)\*\*")
    SECTOR_RE = re.compile(r"\*{0,2}([^|*]{2,6}(?:板块|行业|概念|指数|赛道|题材))")
    INDICATOR_RE = re.compile(r"(MACD|KDJ|RSI|BOLL|OBV|MA\d{2}|MA\d{3})")

    # ── 市场观察模式 ──
    MARKET_PATTERNS = [
        # 大盘环境评分（带 /100）
        (re.compile(r"大盘环境评分[：:]?\s*(\d{1,3})(?:/100)?"), "high"),
        # 指数表现表格行: | 上证指数 | 4041.24 | **-0.06%** | KDJ金叉...
        (re.compile(r"\|\s*\*{0,2}(上证指数|深证成指|创业板指|科创50|沪深300)\*{0,2}\s*\|\s*[\d.]+\s*\|\s*\*{0,2}([+-]?\d+\.?\d*%)\*{0,2}\s*\|\s*([^|]+?)(?=\s*\|)"), "high"),
        # 板块/热点总结行
        (re.compile(r"\|\s*\*{0,2}([^|]{2,10}板块|题材|热点)\*{0,2}\s*\|\s*([^|]+?)(?=\s*\|)"), "medium"),
        # 最强板块/游资方向/题材热度等摘要行
        (re.compile(r"\|\s*(?:🏆\s*)?最强板块|🔥\s*游资方向|📢\s*题材热度|📜\s*宏观\s*\|\s*([^|]+?)(?=\s*\|)"), "high"),
        # 通用: 市场判断（震荡市/牛市/熊市）
        (re.compile(r"(震荡市|牛市|熊市|弱势震荡|强势震荡|多头排列|空头排列)"), "medium"),
        # 技术信号组合（如MACD多头+KDJ金叉）
        (re.compile(r"(MACD(?:多头|空头|金叉|死叉)\+?(?:KDJ|RSI|BOLL)?(?:多头|空头|金叉|死叉)?)"), "high"),
    ]

    # ── 风险发现模式 ──
    RISK_PATTERNS = [
        # 风控表格行: | StockName | 600970.SH | **-16.3%** | 超-7%止损线
        (re.compile(r"\|\s*\*{0,2}([^*|]+?)\*{0,2}\s*\|\s*(\d{6}\.(?:SH|SZ))\s*\|\s*\*{0,2}([+-]?\d+\.?\d*%)\*{0,2}\s*\|\s*(超[^|]*止损|止盈|触发|建议)"), "high"),
        # 风控表格行（带仓位信息）: | **龙净环保** | 🔴 **超限** | 36% → 单票上限10%
        (re.compile(r"\|\s*\*{0,2}([^*|]+?)\*{0,2}\s*\|\s*[🔴🟡]?\s*\*{0,2}(超限|超标|触发)\*{0,2}"), "high"),
        # 仓位超限行: | **100%** | ≤50% | 🔴 超限
        (re.compile(r"\|\s*\*{0,2}(\d+%)\*{0,2}\s*\|\s*[≤<>=]?\s*(\d+%)\s*\|\s*[🔴🟡]?\s*(超限|超标|触发)"), "high"),
        # 移动止损触发: 高点回撤XX%
        (re.compile(r"(?:从\s*高点|高点)?\s*回撤\s*(\d+\.?\d*)%"), "medium"),
        # 风险等级
        (re.compile(r"风险等级[：:]?\s*[🔴🟡🟢]?\s*\*{0,2}(CRITICAL|HIGH|MEDIUM|LOW)\*{0,2}"), "high"),
        # 明确的风险描述（单行匹配，避免跨行吞内容）
        (re.compile(r"\|[^|\n]{0,30}(?:总仓位|持仓|仓位)[^|\n]{0,30}超限[^|\n]*\|"), "high"),
    ]

    # ── 策略调整模式 ──
    STRATEGY_PATTERNS = [
        # 交易指令行: 强制卖出/分批止盈/修正指令
        (re.compile(r"\|\s*\*{0,2}([^*|]+?)\*{0,2}\s*\|\s*(\d{6}\.(?:SH|SZ))\s*\|\s*(全仓|半仓|减半|卖\d/\d)\s*[^(]*?(?:止损|止盈|卖出|减仓)"), "high"),
        # 策略建议
        (re.compile(r"(建议|推荐|调整|提高|降低|改为)\s*(将\s*)?([^，。\n]{2,20}(?:权重|参数|仓位|因子|策略|阈值))"), "medium"),
        # 买入/卖出/加减仓
        (re.compile(r"(买入|卖出|加仓|减仓|建仓|清仓)\s*(?:[^，。\n]{0,20})?\s*(\d+[%成])"), "medium"),
        # 执行后仓位预测
        (re.compile(r"\|\s*总仓位\s*\|\s*\d+\.?\d*%\s*\|\s*~?\*{0,2}(\d+\.?\d*%)\*{0,2}"), "medium"),
    ]

    # ── 模式变化 ──
    PATTERN_PATTERNS = [
        (re.compile(r"(板块|行业|概念|赛道)\s*(轮动|切换|领涨|领跌|排名|强度)"), "medium"),
        (re.compile(r"(因子|策略|指标)\s*(表现|变化|下降|上升|失效|有效|胜率|准确率)"), "medium"),
        (re.compile(r"(科创50|创业板|半导体|新能源|医药|消费|金融)\s*(?:成为|持续|仍然|维持|转为)?\s*(最强|最弱|亮点|领涨|领跌|强势|弱势)"), "high"),
    ]

    # ── 规则更新 ──
    RULE_PATTERNS = [
        (re.compile(r"(规则|参数|权重|阈值)\s*(调整|修改|更新|新建|改为|改成|改为)"), "medium"),
        (re.compile(r"(建议复盘师|建议将)\s*([^，。\n]{2,30})"), "medium"),
    ]

    # 所有模式的类型-模式列表映射
    TYPE_PATTERNS = {
        "market_observation": MARKET_PATTERNS,
        "risk_discovery": RISK_PATTERNS,
        "strategy_adjustment": STRATEGY_PATTERNS,
        "pattern_change": PATTERN_PATTERNS,
        "rule_update": RULE_PATTERNS,
    }

    # ── 标记清除 ──

    @staticmethod
    def _clean_markdown(text: str) -> str:
        """清除 markdown 格式标记"""
        text = re.sub(r'\*{1,2}', '', text)
        text = re.sub(r'[🔴🟡🟢🔥🚀✅❌⚠️ℹ️🏆📊📈📉🛡️🎯📝📜]', '', text)
        text = re.sub(r'\s+', ' ', text).strip()
        return text

    @staticmethod
    def _extract_stock_codes(text: str) -> list[str]:
        """从文本中提取股票代码"""
        return list(set(m.group(0) for m in RuleExtractor.STOCK_CODE_RE.finditer(text)))

    @staticmethod
    def _extract_stock_names(text: str) -> list[str]:
        """从**bold**标记中提取股票/ETF名称（排除百分比和纯数字）"""
        results = []
        for m in RuleExtractor.STOCK_NAME_IN_BOLD.finditer(text):
            name = m.group(1).strip()
            # 排除百分比值、纯数字、emoji开头
            if re.match(r'^[+-]?\d+\.?\d*%?$', name):
                continue
            if not name or len(name) < 2:
                continue
            if any(c in name for c in 'ETF股债基转债'):  # 金融类
                results.append(name)
            elif len(name) >= 2 and not name.startswith(('✅', '❌', '⚠️', '🔴', '🟡', '🟢', '📊', '🏆')):
                results.append(name)
        return list(set(results))

    @staticmethod
    def _extract_sectors(text: str) -> list[str]:
        """提取板块/行业名"""
        result = []
        for m in RuleExtractor.SECTOR_RE.finditer(text):
            name = m.group(1).strip()
            if not any(kw in name for kw in ['止损', '止盈', '仓位', '持仓', '总仓位']):
                result.append(name)
        return result

    @staticmethod
    def _extract_indicators(text: str) -> list[str]:
        """提取技术指标名"""
        return list(set(m.group(1) for m in RuleExtractor.INDICATOR_RE.finditer(text)))

    @staticmethod
    def _extract_tags(text: str) -> list[str]:
        """综合提取所有标签"""
        tags = []
        tags.extend(RuleExtractor._extract_stock_codes(text))
        tags.extend(RuleExtractor._extract_stock_names(text))
        tags.extend(RuleExtractor._extract_sectors(text))
        tags.extend(RuleExtractor._extract_indicators(text))
        return tags[:8]  # 最多8个标签，避免噪声

    @staticmethod
    def _assess_confidence(match_text: str, base_level: str, has_code: bool, has_number: bool) -> str:
        """智能置信度定级

        规则:
          - high: 基础high + (有代码或有明确数字)
          - medium: 有具体数字或代码
          - low: 纯文本匹配
        """
        if base_level == "high" and (has_code or has_number):
            return "high"
        if has_code and has_number:
            return "high"
        if has_number:
            return "medium"
        if has_code:
            return "medium"
        if base_level == "high":
            return "high"
        if base_level == "medium":
            return "medium"
        return "low"

    @staticmethod
    def _generate_name(item_type: str, match_text: str, tags: list[str]) -> str:
        """智能生成名称（清除 markdown + 截断 + 用标签优化）"""
        cleaned = RuleExtractor._clean_markdown(match_text)
        # 标签中只取真正的股票/ETF名（排除百分比数字）
        stock_names = [
            t for t in tags
            if not re.match(r'^[+-]?\d+\.?\d*%?$', t)
            and not re.match(r'\d{6}\.(SH|SZ)', t)
            and len(t) >= 2
        ]
        prefix = ""
        if stock_names:
            prefix = f"[{stock_names[0]}] "
        # 取最精华的部分（| 分隔的表格行取前两段）
        parts = [p.strip() for p in cleaned.split('|') if p.strip()]
        if len(parts) >= 3:
            meaningful = ' | '.join(parts[:3])[:60]
        else:
            meaningful = cleaned[:60]
        if not meaningful:
            meaningful = cleaned[:60]
        name = f"{prefix}{meaningful}" if prefix else meaningful
        return name[:80]

    @classmethod
    def extract(cls, text: str, source_agent: str, source_report: str) -> list[KnowledgeItem]:
        """从文本中提取知识（规则版本 v2 — 智能匹配+置信度+标签）"""
        items = []
        seen_keys: set[str] = set()  # 用归一化内容去重

        for item_type, patterns in cls.TYPE_PATTERNS.items():
            for pattern, base_confidence in patterns:
                for match in pattern.finditer(text):
                    raw_content = match.group(0).strip()
                    if len(raw_content) < 12:
                        continue

                    # ── 归一化去重（清 markdown + 小写 + 缩数字） ──
                    normalized = cls._clean_markdown(raw_content).lower()
                    normalized = re.sub(r'\d+\.?\d*', '#', normalized)  # 数字归一化
                    dedup_key = f"{item_type}:{normalized[:60]}"
                    if dedup_key in seen_keys:
                        continue
                    seen_keys.add(dedup_key)

                    # ── 提取标签和判断特征 ──
                    tags = cls._extract_tags(raw_content)
                    has_code = bool(RuleExtractor.STOCK_CODE_RE.search(raw_content))
                    has_number = bool(re.search(r'\d+\.?\d*', raw_content))

                    # ── 智能置信度 ──
                    confidence = cls._assess_confidence(raw_content, base_confidence, has_code, has_number)

                    # ── 智能名称 ──
                    name = cls._generate_name(item_type, raw_content, tags)

                    # ── 内容精简（去掉多余 markdown） ──
                    content = cls._clean_markdown(raw_content)

                    # ── 如果去重后发现内容太短（清markdown后） ──
                    if len(content) < 8:
                        continue

                    item = KnowledgeItem(
                        item_type=item_type,
                        name=name,
                        content=content,
                        source_agent=source_agent,
                        source_report=source_report,
                        confidence=confidence,
                    )
                    item.tags = tags
                    items.append(item)

        return items


# ===== LLM 提取器 =====

LLM_EXTRACT_PROMPT = """你是一位投研知识库管理员。请从下面的 Agent 报告文本中，提取值得沉淀到知识库的关键信息。

只提取：
1. **市场观察** — 非显而易见的市场模式、资金动向、板块轮动信号
2. **策略调整** — 选股/择时/交易规则的调整建议
3. **风险发现** — 新出现的风险模式、异常信号
4. **模式变化** — 因子表现变化、板块强度转变、规律变化
5. **规则更新** — 需要更新的参数、阈值、条件
6. **表现反馈** — 策略/因子的近期表现反馈

不要提取：
- 显而易见的日常数据（涨跌幅、成交量数字）
- 已经存在于知识库的陈旧知识
- 模糊的、无具体指向的泛泛评论

输出 JSON 数组（每个元素一个发现）:
[
  {{
    "type": "market_observation|strategy_adjustment|risk_discovery|pattern_change|rule_update|performance_note",
    "name": "简短标题（20字内）",
    "content": "详细描述（50-200字）",
    "confidence": "high|medium|low",
    "tags": ["标签1", "标签2"]
  }}
]

如果没有任何可提取的新信息，返回 []。
"""


class LLMExtractor:
    """基于 LLM 的提取器"""

    def __init__(self, llm_client):
        self._llm = llm_client

    def extract(self, text: str, source_agent: str, source_report: str) -> list[KnowledgeItem]:
        """用 LLM 提取知识"""
        if not self._llm or not self._llm.is_available():
            return []

        messages = [
            {"role": "user", "content": f"{LLM_EXTRACT_PROMPT}\n\nAgent: {source_agent}\n报告路径: {source_report}\n\n报告内容:\n{text[:8000]}"},
        ]

        try:
            response = self._llm.complete(
                messages=messages,
                max_tokens=4000,
                temperature=0.3,
                response_format={"type": "json_object"},
            )
            content = self._extract_response_text(response)
            if not content:
                return []

            # 解析 JSON
            parsed = json.loads(content)
            if isinstance(parsed, dict) and "findings" in parsed:
                parsed = parsed["findings"]
            if not isinstance(parsed, list):
                parsed = [parsed]

            items = []
            for item_data in parsed:
                if not isinstance(item_data, dict):
                    continue
                item_type = item_data.get("type", "")
                if item_type not in EXTRACTION_TYPES:
                    continue
                item = KnowledgeItem(
                    item_type=item_type,
                    name=item_data.get("name", "未命名提取")[:60],
                    content=item_data.get("content", ""),
                    source_agent=source_agent,
                    source_report=source_report,
                    confidence=item_data.get("confidence", "medium"),
                )
                item.tags = item_data.get("tags", [])
                items.append(item)

            return items

        except json.JSONDecodeError:
            logger.warning(f"[MemoryExtractor] LLM 返回 JSON 解析失败")
            return []
        except Exception as e:
            logger.warning(f"[MemoryExtractor] LLM 提取异常: {e}")
            return []

    @staticmethod
    def _extract_response_text(response) -> str:
        try:
            return response.choices[0].message.content or ""
        except (AttributeError, IndexError, TypeError):
            pass
        try:
            return response.content[0].text or ""
        except (AttributeError, IndexError, TypeError):
            pass
        return str(response)


# ===== 主提取器 =====

class MemoryExtractor:
    """知识自动提取器

    从 Agent 报告自动提取关键发现，写入 knowledge/。

    Usage:
        extractor = MemoryExtractor()
        result = extractor.extract("20260707")
        print(result.summary())

        # 带 LLM 增强
        extractor = MemoryExtractor(llm_client=LLMClient())
        result = extractor.extract("20260707")
    """

    def __init__(self, llm_client=None):
        self._llm_client = llm_client
        self._rule_extractor = RuleExtractor()
        self._llm_extractor = LLMExtractor(llm_client) if llm_client else None

    # ── 报告收集 ──

    def _find_reports(self, date: str) -> list[tuple[str, str, str]]:
        """查找指定日期的所有 Agent 报告

        Returns:
            [(agent_id, filepath, content), ...]
        """
        results = []
        date_formats = [date, _normalize_date(date)]

        for date_fmt in date_formats:
            for agent_id, pattern in REPORT_PATTERNS:
                full_pattern = pattern.replace("{date}", date_fmt)
                full_path = os.path.join(PROJECT_ROOT, full_pattern)
                files = sorted(glob.glob(full_path))
                for fp in files:
                    # 去重（同一路径不重复加载）
                    if any(fp == existing[1] for existing in results):
                        continue
                    try:
                        content = self._read_file(fp)
                        if content:
                            results.append((agent_id, fp, content))
                    except Exception as e:
                        logger.warning(f"[MemoryExtractor] 读取失败 {fp}: {e}")

        return results

    @staticmethod
    def _read_file(path: str) -> Optional[str]:
        """读取文件，自动回退编码"""
        encodings = ["utf-8", "utf-8-sig", "gbk", "gb2312"]
        for enc in encodings:
            try:
                with open(path, "r", encoding=enc) as f:
                    return f.read()
            except (UnicodeDecodeError, UnicodeError):
                continue
            except Exception as e:
                logger.debug(f"[MemoryExtractor] 读取异常 {path}: {e}")
                return None
        logger.warning(f"[MemoryExtractor] 无法解码 {path}")
        return None

    # ── 知识写入 ──

    def _save_extraction(self, result: ExtractionResult):
        """将提取结果保存到 knowledge/复盘记录/"""
        if not result.items:
            return

        review_dir = KNOWLEDGE_REVIEW_DIR
        os.makedirs(review_dir, exist_ok=True)

        # 写入 JSON 格式（机器可读）
        json_path = os.path.join(review_dir, f"{EXTRACT_FILENAME_PREFIX}{result.date}.json")
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(result.to_dict(), f, ensure_ascii=False, indent=2)
        logger.info(f"[MemoryExtractor] 已保存: {json_path}")

        # 写入 Markdown 格式（人类可读）
        md_path = os.path.join(review_dir, f"{EXTRACT_FILENAME_PREFIX}{result.date}.md")
        lines = [
            f"# 📚 知识提取 — {result.date}",
            f"",
            f"> 报告: {result.reports_loaded} 份 | 提取: {len(result.items)} 条 | LLM: {'✅' if result.llm_used else '❌'}",
            f"",
        ]
        for i, item in enumerate(result.items, 1):
            lines.append("---")
            lines.append(f"## #{i} {item.name}")
            lines.append(item.to_markdown())
        lines.append("")
        lines.append(f"---\n_自动提取于 {datetime.now().strftime('%Y-%m-%d %H:%M')}_")

        with open(md_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        logger.info(f"[MemoryExtractor] 已保存: {md_path}")

    def _update_knowledge_file(self, items: list[KnowledgeItem]) -> int:
        """将特定类型的知识更新到 knowledge/策略/ 对应文件

        Returns: 更新的文件数
        """
        updated = 0
        # 按类型分组
        grouped: dict[str, list[KnowledgeItem]] = {}
        for item in items:
            grouped.setdefault(item.type, []).append(item)

        # 策略更新 → 追加到对应文件
        strategy_map = {
            "strategy_adjustment": "选股策略.md",
            "rule_update": "交易执行规则.md",
            "market_observation": "择时策略.md",
            "pattern_change": "选股策略.md",
            "performance_note": "选股策略.md",
        }

        for item_type, filename in strategy_map.items():
            if item_type not in grouped:
                continue
            filepath = os.path.join(KNOWLEDGE_STRATEGY_DIR, filename)
            if not os.path.exists(filepath):
                continue

            additions = []
            for item in grouped[item_type]:
                additions.append(f"\n---\n> 📝 自动提取 ({_today()}) 来源: {item.source_agent}\n\n{item.content}\n")

            if additions:
                try:
                    with open(filepath, "a", encoding="utf-8") as f:
                        f.writelines(additions)
                    updated += 1
                    logger.info(f"[MemoryExtractor] 追加到 {filename} ({len(additions)} 条)")
                except Exception as e:
                    logger.warning(f"[MemoryExtractor] 写入 {filename} 失败: {e}")

        return updated

    def _update_changes(self, result: ExtractionResult):
        """更新 CHANGES.md"""
        if not result.items:
            return
        try:
            cmd = (
                f'python scripts/utils/knowledge_lint.py --add-change 提取 '
                f'"memory_extractor_{result.date}" '
                f'"自动提取{len(result.items)}条知识({result.reports_loaded}份报告)"'
            )
            import subprocess
            subprocess.run(cmd, cwd=PROJECT_ROOT, shell=True,
                           capture_output=True, timeout=30)
        except Exception as e:
            logger.warning(f"[MemoryExtractor] CHANGES.md 更新失败: {e}")

    # ── 主入口 ──

    def extract(self, date: str = "") -> ExtractionResult:
        """执行知识提取

        Args:
            date: 日期 YYYYMMDD 或 YYYY-MM-DD，默认今天

        Returns:
            ExtractionResult
        """
        date = _normalize_date(date or _today())
        result = ExtractionResult(date)

        # 收集报告
        reports = self._find_reports(date)
        result.reports_loaded = len(reports)
        if not reports:
            result.errors.append(f"未找到 {date} 的报告")
            logger.info(f"[MemoryExtractor] {date}: 无报告可提取")
            return result

        # 对每份报告提取
        all_items: list[KnowledgeItem] = []
        for agent_id, filepath, content in reports:
            # 规则提取（始终运行）
            rule_items = self._rule_extractor.extract(content, agent_id, filepath)
            all_items.extend(rule_items)

            # LLM 增强提取
            if self._llm_extractor:
                try:
                    llm_items = self._llm_extractor.extract(content, agent_id, filepath)
                    if llm_items:
                        all_items.extend(llm_items)
                        result.llm_used = True
                except Exception as e:
                    logger.warning(f"[MemoryExtractor] LLM 提取 {agent_id} 失败: {e}")

        # 去重（规则提取在前，LLM在后；LLM同内容时覆盖规则）
        # 用归一化内容做去重键，LLM提取项优先保留
        seen_contents: dict[str, KnowledgeItem] = {}
        for item in all_items:
            # 归一化键：类型 + 缩数字后的内容
            clean = RuleExtractor._clean_markdown(item.content).lower()
            normalized = re.sub(r'\d+\.?\d*', '#', clean)[:80]
            key = f"{item.type}:{normalized}"

            # 如果是LLM提取的且已存在同内容规则项 → 覆盖
            if item.confidence == "high" or item.source_agent.startswith("llm"):
                seen_contents[key] = item  # LLM/高置信度覆盖
            elif key not in seen_contents:
                seen_contents[key] = item  # 新内容
            # 否则跳过（低置信度不覆盖高置信度）

        for item in seen_contents.values():
            result.add(item)

        # 保存提取结果
        if result.items:
            self._save_extraction(result)
            updated_files = self._update_knowledge_file(result.items)
            self._update_changes(result)
            logger.info(f"[MemoryExtractor] 完成: {result.summary()}")
        else:
            logger.info(f"[MemoryExtractor] {date}: 无可提取的新知识")

        return result


# ===== 便捷函数 =====

def extract_memory(date: str = "",
                   llm_client=None,
                   verbose: bool = False) -> ExtractionResult:
    """快速提取知识"""
    if verbose:
        logging.getLogger().setLevel(logging.INFO)
    extractor = MemoryExtractor(llm_client=llm_client)
    result = extractor.extract(date)
    print(result.summary())
    return result


# ===== CLI =====

def main():
    import argparse
    parser = argparse.ArgumentParser(description="从 Agent 报告自动提取知识")
    parser.add_argument("date", nargs="?", default="", help="日期 YYYYMMDD 或 YYYY-MM-DD")
    parser.add_argument("--llm", action="store_true", help="启用 LLM 增强提取")
    parser.add_argument("--verbose", "-v", action="store_true", help="详细信息")
    args = parser.parse_args()

    if args.verbose:
        logging.basicConfig(level=logging.INFO, format="%(message)s")
    else:
        logging.basicConfig(level=logging.WARNING, format="%(message)s")

    # 初始化 LLM
    llm_client = None
    if args.llm:
        try:
            from scripts.utils.llm_client import LLMClient
            llm_client = LLMClient()
            if not llm_client.is_available():
                print("⚠️  LLM 未配置，使用规则提取")
                llm_client = None
        except Exception as e:
            print(f"⚠️  LLM 初始化失败: {e}")

    result = extract_memory(args.date, llm_client)
    if result.errors:
        print(f"  警告: {'; '.join(result.errors)}")


if __name__ == "__main__":
    main()
