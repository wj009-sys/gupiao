# 🏗️ Karpathy LLM Wiki 工作法

> 来源：[Andrej Karpathy Gist](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f) (2026.04)
> 安装日期：2026-07-03 | 深度研究：2026-07-04
> 核心理念：**"Obsidian 是 IDE，LLM 是程序员，Wiki 是代码库"**

---

## 一、核心思想

### 编译（Compilation）而非检索（Retrieval）

传统 RAG：每次查询都从原始文档中重新检索 → 慢、贵、知识不累积。

LLM Wiki：**一次编译，持久沉淀**。LLM 将原始资料"编译"为结构化的 Markdown Wiki，知识随时间累积而非每次都重新推导。

| 编译器概念 | LLM Wiki 对应 |
|:----------|:-------------|
| 源代码 | `raw/` 目录 — 原始文档 |
| 编译输出 | `wiki/` 目录 — Markdown 页面 |
| 编译器 | **LLM** |
| 构建配置 | **Schema（CLAUDE.md / AGENTS.md）** |
| 增量编译 | 新资料只更新受影响的页面 |
| 依赖图 | 跨引用 Cross-reference |
| Lint/静态分析 | 知识库健康检查 |

### 三句箴言

> **"The LLM writes and maintains the wiki; the human reads and asks questions."**
> LLM 写和维护 Wiki，人类读和提问。

> **"The wiki is a persistent, compounding artifact."**
> Wiki 是持久化、有复利效应的产物。

> **"raw/ 是源代码，LLM 是编译器，wiki/ 是可执行文件，lint 是测试，query 是运行时。"**

---

## 二、三层架构

```
project-root/
├── raw/                    # 第1层：原始资料（不可变）
│   ├── articles/           #   文章
│   ├── papers/             #   论文
│   ├── repos/              #   代码仓库
│   ├── data/               #   数据集
│   └── images/             #   图片
├── wiki/                   # 第2层：编译知识（LLM 维护）
│   ├── index.md            #   主目录（每次操作后更新）
│   ├── log.md              #   只追加的操作日志
│   ├── overview.md         #   总体概览
│   ├── concepts/           #   概念页面（如 attention-mechanism.md）
│   ├── entities/           #   实体页面（如 openai.md）
│   ├── sources/            #   源文件摘要（每摄入一个文档写一个）
│   ├── comparisons/        #   对比页面
│   └── synthesis/          #   跨源综合页面
├── outputs/                # 输出报告、Lint 结果
├── CLAUDE.md               # 第3层：Schema 配置文件（最重要的文件）
└── .gitignore
```

### 第1层：Raw Sources（原始资料，不可变）

LLM **只读不写**。这是事实源头，永远不被修改。

### 第2层：The Wiki（编译知识，LLM 全权维护）

LLM 写入和维护所有 Markdown 页面。人类不直接编辑（通过 LLM 间接管理）。

### 第3层：The Schema（规则约束）

`CLAUDE.md` 定义了 LLM 的行为准则、目录结构、命名规范、工作流程。这是人和 LLM 共同演化的"宪法"。

---

## 三、Vannevar Bush Memex（1945）— 历史渊源

> Karpathy 原文："The idea is related in spirit to Vannevar Bush's Memex (1945) — a personal, curated knowledge store with associative trails between documents. Bush's vision was closer to this than to what the web became: private, actively curated, with the connections between documents as valuable as the documents themselves. **The part he couldn't solve was who does the maintenance. The LLM handles that.**"

LLM Wiki 的本质是 Bush 75年前愿景的现代实现。Memex 缺少的"维护者"角色，终于被 LLM 填补。

---

## 四、为什么这有效

### 维护瓶颈 vs LLM 优势

> "The tedious part of maintaining a knowledge base is not the reading or the thinking — it's the bookkeeping. Updating cross-references, keeping summaries current, noting when new data contradicts old claims, maintaining consistency across dozens of pages. Humans abandon wikis because the maintenance burden grows faster than the value. **LLMs don't get bored, don't forget to update a cross-reference, and can touch 15 files in one pass. The wiki stays maintained because the cost of maintenance is near zero.**"

**人类的工作**：策展原始资料、指导分析方向、提出好问题、思考深层含义。
**LLM 的工作**：其他一切（摘要、交叉引用、归档、簿记）。

### 核心理念：好的回答也可以成为知识

> "The important insight: **good answers can be filed back into the wiki as new pages.** A comparison you asked for, an analysis, a connection you discovered — these are valuable and shouldn't disappear into chat history. This way your explorations compound in the knowledge base just like ingested sources do."

这意味着：每一次有价值的问答都不是一次性的。如果分析出了新洞见，将其保存为新的 wiki 页面，知识库会像摄入源文件一样不断复利增长。

---

## 五、三大核心操作

### 🔄 Ingest（摄入）

添加新资料时的完整流程：

```
1. TRIAGE    → LLM 读取 raw/ 中的新文档
2. DISCUSS   → 向人类提供 1-3 句摘要 + 建议更新的 wiki 页面
3. WRITE     → 创建 wiki/sources/[source-name].md 摘要页面
4. UPDATE    → 更新/创建概念页面、实体页面（级联更新 10-15 个页面）
5. CONSOLIDATE → 矛盾标注为 disputed，记录到 overview "Open Tensions"
6. 更新 wiki/index.md
7. 追加到 wiki/log.md
```

**关键规则**：
- 矛盾标记为 `disputed`，永不静默抹平
- 少数/相反证据保留在综合页面或独立页面中
- 永不修改 `raw/` 中的文件

### ❓ Query（查询）

```
1. 读取 wiki/index.md 找到相关页面
2. 读取那些页面 + wiki/overview.md + wiki/log.md 尾部
3. 基于 wiki 内容合成答案，引用 [[page]] 来源
4. 优先使用 wiki 内容，而非自身训练知识
5. 如果答案有永久价值，保存为新 wiki 页面
6. 存档：短→outputs/queries/，报告→outputs/reports/
7. 更新 outputs/index.md + wiki/log.md
```

### 🔍 Lint（健康检查）

**确定性检查（可自动修复）**：
- 索引一致性：wiki/index.md vs 实际文件系统
- 内部链接：所有 `[[wikilinks]]` 目标必须存在
- Raw 引用：所有 Raw 字段的链接必须指向存在的 raw/ 文件
- See Also：自动补充缺失的交叉引用

**启发式检查（仅报告）**：
- 事实矛盾（跨页面）
- 过时声明（被更新源文件取代）
- 孤页（无入站链接）
- 缺失概念（频繁提到但没有独立页面）
- 争议页面（disputed 标记需要解决）
- 过时内容（最后更新 >3 个月且无新源文件）

---

## 六、日志与索引最佳实践

### index.md（内容导向）

主目录是所有 wiki 页面的分类索引。每条记录包含：链接 + 一句话摘要 + 可选元数据（日期、来源数）。组织方式按类别分组（概念、实体、来源等）。

**规模上限**：~100 个来源、~数百个页面时，纯 index 文件足够，**不需要基于 embedding 的 RAG 基础设施**。

### log.md（时间导向）

只追加的操作日志，记录每次 ingest、query、lint。

**格式技巧**（来自 Karpathy 原文）：

> "if each entry starts with a consistent prefix (e.g. `## [2026-04-02] ingest | Article Title`), the log becomes parseable with simple unix tools — `grep "^## \[" log.md | tail -5` gives you the last 5 entries."

我们项目中的对应：`CHANGES.md` 已使用类似格式 `| 日期 | 类型 | 文件 | 摘要 |` — 同样可用 grep/awk 解析。

---

## 七、可选工具生态

| 工具 | 用途 | 我们的替代 |
|:----|:-----|:----------|
| **Obsidian Web Clipper** | 浏览器扩展，将网页转为 markdown 供摄入 | Agent1 情报采集 |
| **Obsidian 图视图 (Graph View)** | 可视化页面之间的连接关系 | —（暂未使用，未来可嵌入 WebUI） |
| **Marp** | 基于 Markdown 的幻灯片格式 | —（报告使用 Markdown + Word） |
| **Dataview** | Obsidian 插件，基于 frontmatter 的查询引擎 | —（暂未使用） |
| **qmd** | 本地 Markdown 搜索引擎（BM25 + 向量混合 + LLM 重排序），有 CLI 和 MCP | knowledge_lint.py + INDEX.md 检索 |
| **Git** | 版本历史、分支、协作 | ✅ 已在用 |

> 注：qmd 是未来可考虑的增强——当知识库规模显著增长（>500页面）时，支持混合搜索 + LLM 重排序的搜索引擎将超越纯 index 文件的检索能力。

---

## 八、页面规范

### YAML Frontmatter（每个页面必须有）

```yaml
---
title: "页面标题"
type: concept | entity | source-summary | comparison | synthesis | overview
status: active | stale | disputed | archived
sources:
  - raw/papers/filename.md
related:
  - "[[related-concept]]"
created: YYYY-MM-DD
updated: YYYY-MM-DD
confidence: high | medium | low
tags: []
---
```

### 命名规范
- 文件名：kebab-case（如 `attention-mechanism.md`）
- 交叉引用：`[[wikilinks]]` 格式
- 源引用：始终链接回 raw/ 路径
- 输出命名：`outputs/{queries|reports}/YYYY-MM-DD-<slug>.md`

### 原则
- **一页一概念**：页面聚焦，超过 1200 字考虑拆分
- **Git 每次变更**：可回滚、可审计
- **不重复造轮子**：知识已在 wiki 中，先查再用

---

## 九、我们在项目中的落地对照

| LLM Wiki 概念 | 我们项目中对应 | 状态 | 改进方向 |
|:--------------|:--------------|:----:|:--------|
| **raw/ (不可变原始资料)** | `data/raw/` + `data/stocks.db` | ✅ | 数据同步后不修改原始行情 |
| **wiki/ (编译知识)** | `knowledge/` + `memory/` | ✅ | 策略文件 + 复盘记录 |
| **Schema (规则约束)** | `CLAUDE.md` + `skills/*/SKILL.md` | ✅ | D3/D4/D9 三表 |
| **index.md** | `knowledge/INDEX.md` | ✅ | 2026-07-03 新建 |
| **log.md** | `knowledge/CHANGES.md` | ✅ | 2026-07-03 新建 |
| **Lint** | `scripts/utils/knowledge_lint.py` | ✅ | 2026-07-03 新建 |
| **ingest 工作流** | Agent1 情报采集 → Agent2 分析 → ... | ✅ | 7 Agent 流水线 |
| **query 工作流** | 用户提问 → 查知识库 → 合成回答 | ✅ | 但可更正式化 |
| **前端 YAML frontmatter** | 无 | ❌ | 可逐步添加 |
| **3个月过时自动标记** | 无 | ❌ | lint 已支持检测，可加自动标记 |
| **disputed 矛盾标记** | 无但 lint 可检测 | ⚠️ | 矛盾检测已有，缺少 disputed 状态流转 |
| **[[wikilinks]] 跨引用** | memory/ 中有 | ⚠️ | 可扩展到 knowledge/ |

### 我们的差异化优势

Karpathy 的 gist 面向**通用知识管理**，而我们面向**A股投研自动化**，有天然优势：

1. **数据自动更新**：行情数据每日自动同步，不需要手动 "ingest"
2. **Agent 流水线**：7 Agent 闭环本身就是 ingest → compile → verify 的自动化
3. **时间序列维度**：复盘记录天然按时间索引，适合趋势分析
4. **回测验证**：预测 vs 实际对比，本身就是 lint 的验证环节

---

## 十、对我们未来行为的指导

### 每当处理新信息时（Ingest 原则）

1. **原始数据不动** — 行情、财报、新闻的原始数据保存后永不修改
2. **先查知识库，再行动** — 涉及策略/规则判断时，先读 `knowledge/INDEX.md` 找相关文件
3. **新发现写回知识库** — 分析结论、策略调整必须写入 `knowledge/` 对应文件
4. **记录变更** — 更新后调用 `knowledge_lint.py --add-change` 写 CHANGES.md

### 每当回答问题时（Query 原则）

1. **优先引用知识库** — 引用 `knowledge/` 中的策略文件而非依赖训练知识
2. **有价值的回答保存** — 如果分析出了新洞见，写回知识库
3. **标注来源** — 数据引用标注来源（Tushare 接口、计算推导、知识库引用）

### 每当局势不明时（Lint 原则）

1. **跑 lint** — `python scripts/utils/knowledge_lint.py`
2. **检查矛盾** — 策略文件之间是否有不一致的规则
3. **检查过时** — 策略文件是否超过阈值未更新
4. **检查孤页** — 是否有未被引用的文件（可能是废弃或遗漏的）

### 处理分歧时（Disputed 处理）

1. **不掩盖矛盾** — 当不同数据源/策略规则冲突时，标注为 disputed 而非静默选一个
2. **保留所有证据** — 相反观点保留在独立页面或综合页面的 "Open Tensions" 部分
3. **让时间裁决** — 标记为 disputed 后，后续 ingest 的新数据可能自动解决矛盾

---

## 附：原 Gist 精华语录

> "Think of it as **compilation**, not retrieval. The LLM is the compiler; the wiki is the compiled output; raw sources are the source code."

> "At moderate scale (~100 sources, hundreds of pages), there is no need for vector databases or complex RAG infrastructure. The LLM can just read the index and the relevant pages."

> "The bottleneck is not retrieval performance. It is **maintenance cost** — keeping the wiki alive, updated, consistent, and cross-referenced. That's where the LLM shines."

> "You don't need a perfect wiki. You need a wiki that gets **slightly better every time** you interact with it."

> "The schema (CLAUDE.md) is the most important file. It's the **constitution** that governs how the LLM behaves. Invest time in it, and co-evolve it with the LLM."
