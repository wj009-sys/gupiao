---
paths: "knowledge/**/*.md"
---
# 📚 Karpathy Wiki 知识库规范

## 格式要求
- 每个文件必须以 `---` YAML frontmatter 开头
- frontmatter 必须包含：`name`, `description`, `metadata`
- 交叉引用使用 `[[wikilinks]]` 格式
- 超过 1200 字考虑拆分（一页一概念）

## 内容规范
- **原始数据不可变**：data/ 只读不写
- **知识写回持久化**：分析结论必须写入 knowledge/，不能只留在对话中
- **不掩盖矛盾**：冲突时标注 `disputed`，让后续数据裁决

## 更新流程
1. 修改文件 → 2. `python scripts/utils/knowledge_lint.py` 验证 →
3. `python scripts/utils/knowledge_lint.py --add-change <类型> <文件> <描述>`
