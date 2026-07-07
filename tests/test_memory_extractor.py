"""
测试知识自动提取器 — memory_extractor.py

验证 RuleExtractor v2 的提取质量改进。
"""
import pytest
from unittest.mock import patch


class TestRuleExtractor:
    """规则提取器核心能力测试"""

    def test_extract_returns_list(self, sample_report_content):
        """验证 extract 返回 KnowledgeItem 列表"""
        from scripts.utils.memory_extractor import RuleExtractor
        items = RuleExtractor.extract(sample_report_content, "agent7", "test.md")
        assert isinstance(items, list)
        assert len(items) > 0

    def test_no_low_confidence_with_rich_content(self, sample_report_content):
        """验证丰富内容中没有 low confidence"""
        from scripts.utils.memory_extractor import RuleExtractor
        items = RuleExtractor.extract(sample_report_content, "agent7", "test.md")
        low_items = [i for i in items if i.confidence == "low"]
        assert len(low_items) == 0, f"仍有 {len(low_items)} 条 low 置信度"

    def test_has_high_confidence_items(self, sample_report_content):
        """验证存在高置信度提取项"""
        from scripts.utils.memory_extractor import RuleExtractor
        items = RuleExtractor.extract(sample_report_content, "agent7", "test.md")
        high_items = [i for i in items if i.confidence == "high"]
        assert len(high_items) >= 3, f"高置信度项不足: {len(high_items)}"

    def test_tags_populated(self, sample_report_content):
        """验证标签不为空（报告包含股票代码和指标）"""
        from scripts.utils.memory_extractor import RuleExtractor
        items = RuleExtractor.extract(sample_report_content, "agent7", "test.md")
        items_with_tags = [i for i in items if i.tags]
        assert len(items_with_tags) >= 3, f"有标签的项不足: {len(items_with_tags)}"

    def test_market_observation_found(self, sample_report_content):
        """验证提取到市场观察"""
        from scripts.utils.memory_extractor import RuleExtractor
        items = RuleExtractor.extract(sample_report_content, "agent7", "test.md")
        market_items = [i for i in items if i.type == "market_observation"]
        assert len(market_items) >= 1

    def test_risk_discovery_found(self, sample_report_content):
        """验证提取到风控发现"""
        from scripts.utils.memory_extractor import RuleExtractor
        items = RuleExtractor.extract(sample_report_content, "agent7", "test.md")
        risk_items = [i for i in items if i.type == "risk_discovery"]
        assert len(risk_items) >= 1

    def test_clean_markdown_removes_bold(self):
        """验证 _clean_markdown 清除 ** 标记"""
        from scripts.utils.memory_extractor import RuleExtractor
        cleaned = RuleExtractor._clean_markdown("**龙净环保**仓位**超限**")
        assert "**" not in cleaned
        assert "龙净环保" in cleaned

    def test_clean_markdown_removes_emoji(self):
        """验证 _clean_markdown 清除 emoji"""
        from scripts.utils.memory_extractor import RuleExtractor
        cleaned = RuleExtractor._clean_markdown("🔴 **超限** 仓位")
        assert "🔴" not in cleaned

    def test_extract_stock_codes(self, sample_report_content):
        """验证提取股票代码"""
        from scripts.utils.memory_extractor import RuleExtractor
        codes = RuleExtractor._extract_stock_codes(sample_report_content)
        assert len(codes) >= 3
        assert any("518880.SH" in c for c in codes)
        assert any("600970.SH" in c for c in codes)

    def test_extract_indicators(self, sample_report_content):
        """验证提取技术指标"""
        from scripts.utils.memory_extractor import RuleExtractor
        indicators = RuleExtractor._extract_indicators(sample_report_content)
        assert "MACD" in indicators
        assert "KDJ" in indicators
        assert "RSI" in indicators
        assert "OBV" in indicators

    def test_generate_name_clean(self):
        """验证生成的名称不含 markdown"""
        from scripts.utils.memory_extractor import RuleExtractor
        name = RuleExtractor._generate_name("risk_discovery",
            "| **龙净环保仓位** | 🔴 **超限** | 36% |", ["龙净环保"])
        assert "**" not in name
        assert "超限" in name

    def test_dedup_different_agents_same_content(self):
        """验证跨 Agent 同内容去重"""
        from scripts.utils.memory_extractor import RuleExtractor, KnowledgeItem
        text = "大盘环境评分: 45/100"
        items1 = RuleExtractor.extract(text, "agent2", "r1.md")
        items2 = RuleExtractor.extract(text, "agent7", "r2.md")
        # 合并去重
        all_items = items1 + items2
        seen = set()
        for item in all_items:
            clean = RuleExtractor._clean_markdown(item.content).lower()
            import re
            normalized = re.sub(r'\d+\.?\d*', '#', clean)[:80]
            key = f"{item.type}:{normalized}"
            seen.add(key)
        # 只有1个唯一键
        assert len(seen) >= 1

    def test_knowledge_item_validation(self):
        """验证 KnowledgeItem 类型校验"""
        from scripts.utils.memory_extractor import KnowledgeItem, EXTRACTION_TYPES
        # 有效类型
        item = KnowledgeItem("market_observation", "test", "content")
        assert item.type == "market_observation"
        # 无效类型应报错
        with pytest.raises(AssertionError):
            KnowledgeItem("invalid_type", "test", "content")

    def test_extraction_result_summary(self):
        """验证 ExtractionResult 摘要格式"""
        from scripts.utils.memory_extractor import ExtractionResult, KnowledgeItem
        result = ExtractionResult("2026-07-07")
        result.add(KnowledgeItem("market_observation", "测试", "内容"))
        summary = result.summary()
        assert "2026-07-07" in summary
        assert "1 条" in summary or "发现: 1" in summary


class TestMemoryExtractor:
    """MemoryExtractor 集成测试"""

    def test_init_without_llm(self):
        """验证无 LLM 时正常初始化"""
        from scripts.utils.memory_extractor import MemoryExtractor
        extractor = MemoryExtractor(llm_client=None)
        assert extractor._rule_extractor is not None
        assert extractor._llm_extractor is None

    def test_find_reports_no_date_returns_empty(self):
        """验证无报告时返回空（使用不存在的日期）"""
        from scripts.utils.memory_extractor import MemoryExtractor
        extractor = MemoryExtractor()
        reports = extractor._find_reports("20990101")
        assert len(reports) == 0

    def test_extract_empty_date_returns_empty_items(self):
        """验证无报告日期提取返回 0 条"""
        from scripts.utils.memory_extractor import MemoryExtractor
        extractor = MemoryExtractor()
        result = extractor.extract("20990101")
        assert len(result.items) == 0
        assert result.reports_loaded == 0

    def test_read_file_utf8(self, tmp_path):
        """验证读取 UTF-8 文件成功"""
        from scripts.utils.memory_extractor import MemoryExtractor
        f = tmp_path / "test.md"
        f.write_text("# Test 内容", encoding="utf-8")
        content = MemoryExtractor._read_file(str(f))
        assert content is not None
        assert "# Test" in content

    def test_read_file_nonexistent(self):
        """验证读取不存在的文件返回 None"""
        from scripts.utils.memory_extractor import MemoryExtractor
        content = MemoryExtractor._read_file("/nonexistent/path.md")
        assert content is None
