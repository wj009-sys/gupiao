# 股票投研自动化项目

## ⚡ 行为准则 — Karpathy LLM Wiki 工作法（2026-07-03安装）

本项目遵循 **Karpathy LLM Wiki 范式**：原始资料不可变、知识编译沉淀、LLM维护知识库。
详见 `knowledge/LLM-Wiki-工作法.md`（工作规范，非策略文件）。

### 核心原则（每次交互遵循）

1. **原始数据不可变** — `data/`、`data/raw/`、`data/stocks.db` 的原始数据只读不写。
   行情数据一旦同步，永不修改。只通过追加新数据更新。

2. **知识写回持久化** — 分析结论、策略调整、新发现的模式，必须写入 `knowledge/` 对应文件，
   不能只存在于对话上下文中。这是知识复利的基础。

3. **先查知识库，再行动** — 涉及策略/规则/参数判断时，先读 `knowledge/INDEX.md` 定位相关文件，
   再读取具体策略文件。优先引用知识库而非训练知识。

4. **记录每次变更** — 更新知识库后调用 `python scripts/utils/knowledge_lint.py --add-change 类型 文件 描述`
   写入 `knowledge/CHANGES.md`。所有变更可追溯。

5. **定期自检** — 每周至少跑一次 `python scripts/utils/knowledge_lint.py`，检查孤页、断裂引用、
   规则矛盾、过时文件。健康度必须维持 🟢 绿色。

6. **不掩盖矛盾** — 数据源/策略规则冲突时，标注为 `disputed` 而非静默选择一个。
   相反证据保留在综合页面，让后续数据自动裁决。

7. **有价值的回答保存** — 如果分析/问答产出了新洞见，写入知识库供未来复用。
   好答案不应只存在于一次对话中。

8. **一页一概念** — 知识库页面保持聚焦，超 1200 字考虑拆分。
   交叉引用使用 `[[wikilinks]]` 格式。

### 知识库三层架构

| 层 | 路径 | 规则 |
|:---|:-----|:-----|
| **Raw（原始资料）** | `data/` + `data/raw/` + `data/stocks.db` | **只读不写**，LLM 永不修改 |
| **Wiki（编译知识）** | `knowledge/` + `memory/` | **LLM 维护**，分析结论写回这里 |
| **Schema（行为规则）** | `CLAUDE.md` + `skills/*/SKILL.md` | **共同演化**，人+LLM 持续优化 |

> 整个项目本身就是 Karpathy 范式的 A 股投研特化版：
> 10 Agent 流水线 = 自动化的 ingest → compile → verify → lint 闭环。
> 每日复盘 = 内置的 lint + synthesis 机制。

## 项目目标

构建A股自动化投研团队，包含10个AI Agent角色（9个流水线Agent + 1个交互式问股Agent），每天自动完成情报采集→技术分析→政策分析→游资追踪→选股推荐→风控检查→交易计划→复盘迭代的完整闭环，由投资领导统筹管理。每个Agent都经过Darwin Skill优化（评分从平均65.1提升至97.1，全部达到五星标准）。

## 🔄 数据自动同步

每次打开 Claude Code 时，`SessionStart` hook 自动运行 `python scripts/utils/auto_sync.py --check-only`，检查 `data/stocks.db` 数据新鲜度（<1秒），结果自动注入会话上下文。

### 自动同步架构
```
Cron: 每天18:03 (工作日)
  python auto_sync.py --auto-sync  ← 主力：收盘后自动拉全量
        │
        ▼
SessionStart Hook (startup)
  python auto_sync.py --check-only ← 备份：打开时快速检查
  如 STALE → AI 主动触发 --auto-sync

同步流水线（13个阶段）:
  [0/13] fund_basic         — ETF元数据（仅首次）
  [1/13] index_basic        — 指数元数据（仅首次）
  [2/13] daily_basic        — 每日估值（按日期批量 ⚡）
  [3/13] index_daily        — 全部指数日线（~3.5min/天）
  [4/13] etf_daily          — ETF日线（~9min/天）
  [5/13] daily_price        — 股票日线（~22min/天）
  [6/13] adj_factor         — 复权因子
  [7/13] fina_indicator     — 财报（季度）
  [8/13] dividend           — 分红
  [9/13] ths_daily          — 概念板块
  [10/13] moneyflow_hsgt    — 北向资金
  [11/13] moneyflow_stock   — 个股资金流向（仅持仓+自选）
  [12/13] daily_indicator   — 全市场技术指标计算
  [13/13] quality_check     — 覆盖度检查+多源回补+数据清洗
                            ├─ A. 覆盖率检查（股票/ETF/指数 vs 预期）
                            ├─ B. 多源回补（Tushare→mootdx→Akshare）
                            └─ C. 数据清洗（去重+NULL填充+异常标记）
```

### 手动同步命令
```bash
# 仅检查（<1秒，纯SQL）
python scripts/utils/auto_sync.py --check-only

# 自动同步所有缺失数据（默认45分钟预算）
python scripts/utils/auto_sync.py --auto-sync

# 限时/限量同步
python scripts/utils/auto_sync.py --auto-sync --max-minutes 15 --max-stocks 500
```

## 报告格式规范

| 送达渠道 | 格式 | 机制 |
|---------|------|------|
| 📁 本地存储 | Markdown (.md) + Word (.docx) | Python脚本生成，`md_to_docx.py`自动转换 |
| 💬 微信推送（主通道） | **Markdown 原文** | `PushPlus API` → PushPlus 公众号推送到微信。`wechat_send.py` 默认通道 |
| 📱 微信推送（备选） | Word (.docx) 文件 | `cc-connect send --file` 通过 ilink API，当 PushPlus 不可用时备用 |

> **默认推送通道**：PushPlus API（已配置 `PUSHPLUS_TOKEN`）。
> 使用 `python scripts/utils/wechat_send.py --report <类型>` 自动推送。
> 如需强制走 cc-connect（ilink），加 `--cc-connect` 参数。
> 如 context_token 过期，运行 `--watch` 模式等待用户消息后自动重试。

## Agent 团队

| Agent | Skill | 脚本 | 达成分数 | 模型 | 定时 |
|-------|-------|------|---------|------|------|
| 🕵️ Agent1 情报员 | `skills/agent1-情报员/SKILL.md` | `fetch_all.py` | **96** (Darwin五星) | 默认 | 07:00 |
| 📊 Agent2 分析师 | `skills/agent2-分析师/SKILL.md` | `analyze.py` | **97** (Darwin五星) | 默认 | 08:30 |
| 🛡️ Agent3 风控官 | `skills/agent3-风控官/SKILL.md` | `risk_check.py` | **97** (Darwin五星) | 默认 | 按需 |
| 🔄 Agent4 复盘师 | `skills/agent4-复盘师/SKILL.md` | `review.py` | **97** (Darwin五星) | **opus** | 21:00 |
| 🔍 Agent5 选股机器人 | `skills/agent5-选股机器人/SKILL.md` | `stock_picker.py` | **100** (Darwin五星) | 默认 | 09:00/12:00/21:30 |
| 🎯 Agent6 操盘手 | `skills/agent6-操盘手/SKILL.md` | `trader.py` | **97** (Darwin五星) | 默认 | 按需 |
| 🏆 Agent7 投资领导 | `skills/agent7-投资领导/SKILL.md` | `leader.py` | **96** (Darwin五星) | **opus** | 按需 |
| 📜 Agent8 政策分析师 | `skills/agent8-政策分析师/SKILL.md` | `policy_analyst.py` | **—** (待评分) | 默认 | 07:30 |
| 🔥 Agent9 游资追踪师 | `skills/agent9-游资追踪师/SKILL.md` | `hot_money_tracker.py` | **—** (待评分) | 默认 | 08:00 |

| 🔮 Agent-问股 | `skills/agent-问股/SKILL.md` | `agent_ask/ask.py` | **—** (Darwin待评分) | 默认 | 按需 |

### Agent 模型分配

Agent 的 SKILL.md frontmatter 中通过 `model:` 字段声明所需模型。
模型选择由调用方读取 SKILL.md 决定，仅复杂推理型 Agent 使用 Opus：

| Agent | 模型 | 原因 |
|-------|------|------|
| Agent1 情报员 | 默认 | 结构化数据采集，不需要复杂推理 |
| Agent2 分析师 | 默认 | 公式计算+指标生成，不需要复杂推理 |
| Agent3 风控官 | 默认 | 规则引擎判断，不需要复杂推理 |
| **Agent4 复盘师** | **opus** | 深度复盘分析、多Agent偏差对比、知识库合成、模式发现 |
| Agent5 选股机器人 | 默认 | 多因子评分+排序，结构化流水线 |
| Agent6 操盘手 | 默认 | 基于规则+约束生成交易计划，不需要复杂推理 |
| **Agent7 投资领导** | **opus** | 质量审核（6Agent结构化评判）、冲突仲裁、最终投资决策 |
| Agent8 政策分析师 | 默认 | 政策数据采集+分类，结构化处理，不需要复杂推理 |
| Agent9 游资追踪师 | 默认 | 龙虎榜数据分析+情绪计算，结构化处理，不需要复杂推理 |
| Agent-问股 | 默认 | 策略模板匹配+技术指标解释，不需要复杂推理 |

> Agent4 和 Agent7 在各自 `skills/agent4-复盘师/SKILL.md` 和 `skills/agent7-投资领导/SKILL.md`
> 的 frontmatter 中声明了 `model: opus`。其余 8 个 Agent 不声明 model 字段，由调用方使用默认模型。

### 优化后新增通用模块（所有Skill均含）

| 模块 | 说明 | 来源 |
|-----|------|------|
| 🚨 异常处理表 | 每个数据源/步骤的「触发条件→一线修复→仍失败兜底」三段式 | Darwin D3 |
| 🔴 CHECKPOINT | 关键决策前的数据完整性/结论可验证性检查点 | Darwin D4 |
| ⛔ 工作反例 | 「不要这样做」的黑名单清单（含为什么+应该怎么做） | Darwin D9 |

### Agent1：情报员（信息采集）
- **职责**：每天早7点自动抓取全网财经资讯（政策、公告、龙虎榜、资金流向）
- **输出**：`reports/日报/情报/情报摘要_YYYY-MM-DD.md`
- **工具**：WebFetch + WebSearch + Tushare
- **D3异常**：10条fallback（新增排序失败、多数据源降级、报告写入三连试）
- **D4检查**：7个CP（数据完整性检查、多源一致性验证、持仓时效标注）
- **D9反例**：8条（新增排序方向错误、工具单一、超长报告）

### Agent2：分析师（技术分析）
- **职责**：基于情报拉取行情，计算MACD/KDJ/RSI/布林带，板块强度排名
- **输出**：`reports/日报/分析/分析报告_YYYY-MM-DD.md`
- **指标参数**：MACD(12,26,9), KDJ(9,3,3), RSI(14), BOLL(20)
- **D3异常**：7条fallback（新增环境评分偏差、情报缺失、科创50缺失）
- **D4检查**：6个CP（强烈信号双重验证、指标长度检查、板块时效标注）
- **D9反例**：10条（新增超卖=买入、数据不足强行计算、忽略科创50、忽略板块轮动）

### Agent3：风控官（风险管理 + 操盘审查）
- **职责**：大盘环境评估、止损线检查、仓位超限监控、**审查操盘手交易计划（买入/卖出清单风控审批）**、**独立持仓加减仓建议**、**与操盘手分歧时上报投资领导仲裁**
- **输出**：风控提醒（`reports/日报/风控/`）+ 操盘计划审查章节 + 冲突项汇总
- **规则文件**：`data/仓位管理规则.json`, `data/止损规则.json`
- **D3异常**：11条fallback（新增测试持仓、环境评分缺失、北向资金、high/low字段缺失、行业分类不准）
- **D4检查**：11个CP（新增移动止损校验、行业分类验证、high/low字段检查）
- **D9反例**：10条（新增测试数据当真、移动止损用当日涨跌幅、忽略MA20/MA60联动）

### Agent4：复盘师（自我进化）
- **职责**：每晚9点复盘当天研判，对比预测vs实际，更新知识库
- **输出**：`reports/日报/复盘/复盘报告_YYYY-MM-DD.md`
- **维护**：`knowledge/策略/` + `knowledge/复盘记录/`
- **D3异常**：11条fallback（新增停牌/退市、预测提取混乱、板块匹配失败等）
- **D4检查**：9个CP（偏差改进措施验证、知识库更新确认、因子表现统计）
- **D9反例**：11条（新增只记录不执行、不验证选股推荐、不追踪Agent1/3效能）

### Agent5：选股机器人（多因子选股）
- **职责**：基于情报热点+技术面+基本面因子，筛选候选股票池。支持4种选股模式：早盘/盘中/午盘/晚间
- **输出**：`reports/日报/选股/选股建议_YYYY-MM-DD.md`（各模式不同格式）
- **脚本参数**：`python stock_picker.py --mode pre_market|intraday|noon|evening --top-n 5`
- **定时**：09:00早盘 / 12:00午盘 / 21:30晚间选股；盘中按需手动触发
- **配置**：`data/选股规则.json`（含4种模式独立权重） + `knowledge/策略/选股策略.md`
- **D3异常**：13条fallback（新增模式选择错误、停复牌失败、盘中数据不可用等）
- **D4检查**：8个CP（新增模式匹配检查、模式特有检查）
- **D9反例**：11条（新增早盘用晚间权重、盘中看估值、不看复盘偏差等）

### Agent6：操盘手（交易计划）
- **职责**：基于选股建议+风控约束+仓位规则，制定交易计划，**接受风控官审查并按意见修改**
- **输出**：`reports/日报/操盘/交易计划_YYYY-MM-DD.md`
- **配置**：`data/仓位管理规则.json` + `knowledge/策略/交易执行规则.md`
- **D3异常**：11条fallback（新增仓位异常、超卖、涨跌停超限、熔断/临停等）
- **D4检查**：10个CP（买入合规、止损必设、总仓位上限、涨跌停检查、仓位上限歧义处理）
- **D9反例**：12条（新增卖不留余量、否决不改计划、价格超涨跌停、正则张冠李戴）
- **制衡机制**：交易计划由风控官（Agent3）审查，分歧由投资领导（Agent7）仲裁

### Agent7：投资领导（团队管理 + 质量审核 + 冲突仲裁）
- **职责**：统筹调度所有Agent，分工派活、**结构化审核每个Agent输出质量（不合格打回重做）**、**仲裁风控官vs操盘手冲突**、最终决策
- **输出**：`reports/日报/决策/投资决策_YYYY-MM-DD.md`（含质量审核章节、打回重做指令、冲突仲裁章节）
- **管理对象**：Agent1-6全部归属投资领导调度
- **质量审核**：每Agent专属审核标准（情报员6项、分析师5项、选股机器人5项、风控官5项、操盘手5项），含必须项和建议项
- **打回重做**：不合格报告附带具体改进要求，重做后通过 `#REWORKED` 标记验证
- **仲裁机制**：风控官否决操盘手交易时，投资领导逐项裁定（风控一票否决/折中部分止盈/有条件放行）
- **D3异常**：14条fallback（新增大量Agent未运行、策略过时、因子调整过多等）
- **D4检查**：12个CP（新增全部Agent未运行检查、策略时效验证、因子调整合理性）
- **D9反例**：16条（新增替Agent做事、只审不反馈、和稀泥仲裁、持仓为空出指令）

---
## 风控制衡机制

Agent3（风控官）与 Agent6（操盘手）构成「提案-审查」双轨制，Agent7（投资领导）负责质量审核和冲突仲裁：

```
各Agent生成报告 → Agent7 质量审核
                      ↓
               ✅ 通过 → 纳入最终决策
               ⚠️ 需补充 → 打回补充→添加#REWORKED→重审
               ❌ 不合格 → 打回重做→添加#REWORKED→重审
               🔴 放弃 → 跳过该Agent，标注原因
```

操盘手与风控官的交易分歧仲裁：

```
操盘手 → 制定交易计划 → 风控官审查
                            ↓
                     🟢批准 → 正常执行
                     🟡有条件 → 按条件修改
                     🔴否决 → 进入观察池
                     🔴分歧 → 上报投资领导仲裁
                               ↓
                          Agent7 最终裁定
```

**关键规则：**
1. 风控官的独立意见和操盘手的交易计划并行，互不隶属
2. 意见一致时放行，意见分歧时投资领导（Agent7）裁定
3. 风控等级 HIGH 时，风控官拥有默认最高优先级（一票否决）
4. 仲裁结论必须写入最终决策报告，否决项必须从执行清单移除
5. 投资领导对其他所有Agent的输出进行质量审核，不合格打回重做
6. 重做完成的Agent在报告末尾添加 `#REWORKED` 标记供自动验证

## 微信消息处理流程

所有来自微信（cc-connect）的消息默认由投资领导（Agent7）处理。

### 消息处理流水线
```
用户微信消息 → 投资领导(Agent7) 接收
                   ↓
            判断需求类型
             ├─ 📊 情报/行情 → 派给Agent1情报员
             ├─ 📈 分析/板块 → 派给Agent2分析师
             ├─ 🛡️ 风控/止损 → 派给Agent3风控官
             ├─ 🔄 复盘/总结 → 派给Agent4复盘师
             ├─ 🔍 选股/推荐 → 派给Agent5选股机器人
             ├─ 🔮 问股/分析 → 派给Agent-问股
             ├─ 📜 政策/宏观 → 派给Agent8政策分析师
             ├─ 🔥 游资/资金 → 派给Agent9游资追踪师
             ├─ 🎯 下单/交易 → 派给Agent6操盘手
             ├─ 🏆 综合/决策 → 自己做最终决策
             └─ 💬 闲聊/简单 → 直接回复
                   ↓
            Agent执行任务 → 返回结果
                   ↓
            投资领导质量审核
             ├─ ✅ 通过 → 回复用户
             ├─ ⚠️ 需补充 → 打回要求补充+#REWORKED→重审
             └─ ❌ 不合格 → 打回重做+#REWORKED→重审
```

### 配置
- `cc-connect.toml` 中 `append_system_prompt` 指定投资领导角色注入
- 新会话自动加载，当前会话需重启daemon后生效

## 双向反馈闭环

整个团队形成「投资领导→审核Agent→复盘师→反审核投资领导」的双向反馈闭环：

```
                        投资领导(Agent7)
                       /    |    |    \
              ┌─── Agent1  Agent2  ...  Agent6
              ↓         \    |    |    /
           复盘师(Agent4) ←─────── 输出报告
              ↓
        审核投资领导工作
         ├─ 分工合理性
         ├─ 审核质量
         ├─ 决策准确性
         ├─ 打回合理性
         └─ 改进建议
              ↓
        投资领导收到反馈 → 改进下一轮工作
         └─ 同样错误不犯第二次
```

### 复盘师对投资领导的审核范畴
| 维度 | 审核内容 |
|------|---------|
| 🎯 决策质量 | 决策逻辑是否清晰、数据引用是否准确、结论是否可执行 |
| 👥 分工合理性 | 是否派对了正确的Agent、执行顺序是否合理、有无遗漏 |
| 🔍 审核质量 | 是否按标准逐项审核、打回要求是否明确、#REWORKED是否跟踪 |
| 📊 决策准确性 | 买入/卖出/持有建议的实际结果、仲裁结论是否正确 |
| 📈 领导成长 | 前次指出的问题本次是否改正、有无重复犯错 |

### 投资领导收到反馈后的处理
- ✅ **认可** → 纳入改进计划，后续执行中注意
- ⚠️ **有异议** → 记录争议点，下次复盘时回应
- 📝 **核心原则**：同样的错误不犯第二次。复盘师的反馈是帮你进步的，不是找你麻烦的。

### Agent8：政策分析师（政策影响评估 — 吸收TradingAgents-astock Policy Analyst）
- **职责**：每天早7:30自动采集政策新闻（宏观/产业/监管/税收），评估政策对持仓的影响
- **上游输入**：Agent1 情报员（提供政策线索）
- **下游输出**：Agent2 分析师（宏观背景）、Agent7 投资领导（纳入决策）
- **输出**：`reports/日报/政策/政策分析_YYYY-MM-DD.md`
- **数据源**：东方财富新闻API（em_get()限流）、财联社、央行/证监会公告
- **D3异常**：6条fallback（新闻API无响应、财联社无法访问、分类失败、持仓缺失、北向资金不可用、DB写入失败）
- **D4检查**：5个CP（来源验证、持仓关联、影响一致性、历史去重、输出完整）
- **D9反例**：5条（只收集不分析、忽视政策节奏、脱离行情背景）

### Agent9：游资追踪师（资金面分析 — 吸收TradingAgents-astock Hot Money Tracker）
- **职责**：每天早8:00分析龙虎榜数据，追踪知名游资席位，计算资金情绪指数
- **上游输入**：Agent1 情报员（龙虎榜基础数据）、Agent8 政策分析师（政策影响资金情绪）
- **下游输出**：Agent2 分析师（资金面辅助）、Agent5 选股机器人（游资方向因子）、Agent6 操盘手（资金面风险）
- **输出**：`reports/日报/游资/游资追踪_YYYY-MM-DD.md`
- **数据源**：Tushare `limit_list`/`top_list`、东方财富龙虎榜API（em_get()）、Tushare `moneyflow`（个股资金）
- **D3异常**：5条fallback（龙虎榜为空、top_list API不可用、资金流向为空、持仓缺失、东财API 403）
- **D4检查**：5个CP（双源验证、席位风格标注、持仓全覆盖、情绪有依据、数据时效）
- **D9反例**：5条（涨跌停板当龙虎榜、游资机构不分、忽视连板效应）

## 目录结构

```
.mcp.json            - MCP 服务器配置（已清空，使用内置 Cron 工具替代）
.env.example         - 环境变量模板
docs/archive/        - 历史设计文档和过时脚本归档
data/              - 数据文件（持仓、自选、规则配置、数据库）
  ├── stocks.db       - SQLite主数据库（~7000万行，23张表+20索引）
  ├── trading_calendar.json - 交易日历缓存
  ├── checkpoints/    - 数据同步检查点（gitignored）
  ├── raw/            - 原始数据缓存（gitignored）
reports/           - 报告输出（日报/周报/月报，日报文件已 gitignored）
  ├── 日报/情报/   - Agent1 情报摘要
  ├── 日报/分析/   - Agent2 分析报告
  ├── 日报/选股/   - Agent5 选股建议
  ├── 日报/风控/   - Agent3 风控报告
  ├── 日报/操盘/   - Agent6 交易计划
  ├── 日报/决策/   - Agent7 投资决策
  ├── 日报/复盘/   - Agent4 复盘报告
  ├── 日报/政策/   - Agent8 政策分析
  ├── 日报/游资/   - Agent9 游资追踪
  ├── 周报/        - 每周汇总
  └── 月报/        - 每月汇总
scripts/           - Python 分析脚本
  ├── agent1-情报采集/
  ├── agent2-技术分析/
  ├── agent3-风控/
  ├── agent4-复盘/
  ├── agent5-选股/
  ├── agent6-操盘/
  ├── agent7-决策/
  ├── agent8-政策分析/  - Agent8 政策分析师（吸收 TradingAgents-astock Policy Analyst）
  ├── agent9-游资追踪/  - Agent9 游资追踪师（吸收 TradingAgents-astock Hot Money Tracker）
  ├── agent_ask/        - Agent问股（自然语言策略问股）
  └── utils/            - 工具函数（Tushare/AkShare/mootdx数据源、em_get()限流网关、技术指标、L2重排序、L3后置分析、风险叠加、RPS、DB管理、自动同步等）
webui/             - Web界面（FastAPI，策略问股可视化）
.github/workflows/  - GitHub Actions CI/CD（定时运行全Agent流水线）
.claude/           - Claude 配置（完整Claude Code集成）
  ├── mcp-servers/     - 旧版Node.js MCP服务器（已弃用）
  ├── hooks/           - Hook 脚本（PreToolUse/Stop/PreCompact，Python实现）
  │   ├── protect-knowledge.py  - 🔒 知识库安全保护（PreToolUse）
  │   ├── save-session-summary.py - 📋 会话摘要保存（Stop）
  │   └── save-snapshot.py       - 💾 压缩前快照（PreCompact）
  ├── agents/          - 自定义子代理定义
  │   ├── deep-auditor.md        - 🔍 全项目深度审计Agent (opus)
  │   ├── code-reviewer.md       - 👨‍💻 Python代码审查Agent (sonnet)
  │   └── research-synthesizer.md - 📊 研究综合Agent (opus)
  ├── workflows/       - 自动化工作流
  │   └── darwin-13-health-check.js - 🩺 项目健康度检查工作流
  ├── settings.json    - 项目共享配置（hooks + permissions）
  ├── settings.local.json - 本地凭据与Token（gitignored）
  ├── rules/           - 路径作用域规则（agent-scripts/knowledge-files/database-access）
  └── scheduled_tasks.json - 定时任务存储
knowledge/         - 知识库（Agent4 维护更新）
  ├── 策略/        - 选股/择时/交易策略
  │   ├── 选股策略.md
  │   ├── 择时策略.md
  │   └── 交易执行规则.md
  └── 复盘记录/    - 历史复盘
memory/            - 📁 项目级记忆（决策反思/风控制衡/质量审核，4文件）
                     📁 持久记忆见: C:\Users\65004\.claude\projects\...\memory\ (MEMORY.md索引，16文件)
skills/            - 自定义 Skills
  ├── agent1-情报员/SKILL.md + test-prompts.json
  ├── agent2-分析师/SKILL.md + test-prompts.json
  ├── agent3-风控官/SKILL.md + test-prompts.json
  ├── agent4-复盘师/SKILL.md + test-prompts.json
  ├── agent5-选股机器人/SKILL.md + test-prompts.json
  ├── agent6-操盘手/SKILL.md + test-prompts.json
  ├── agent-问股/SKILL.md
  ├── agent7-投资领导/SKILL.md + test-prompts.json
  ├── agent8-政策分析师/SKILL.md
  └── agent9-游资追踪师/SKILL.md
```

## 数据源

- **Tushare Pro**：A股行情、财务、龙虎榜、资金流向（token通过 `.claude/settings.local.json` 自动加载，不硬编码）
- **AkShare**：A股行情后备数据源（Tushare不可用时自动fallback），通过 `data_provider.py` 统一接口调用
- **网页抓取**：财联社、东方财富、巨潮资讯

## 策略知识库

| 文件 | 用途 | 维护者 |
|------|------|--------|
| `knowledge/策略/选股策略.md` | 多因子选股权重、筛选参数 | Agent5 + Agent4 |
| `knowledge/策略/择时策略.md` | 入场/出场时机、大盘联动 | Agent6 + Agent4 |
| `knowledge/策略/交易执行规则.md` | 买卖规范、仓位分配、止盈止损规则 | Agent6 + Agent4 |
| `knowledge/策略/ZhuLinsen三项目借鉴分析.md` | 借鉴分析+架构参考 | Agent4 |
| `knowledge/策略/TradingAgents借鉴分析.md` | Multi-Agent架构参考 | Agent4 |
| `knowledge/策略/MA96-RSI策略分析.md` | MA96+RSI策略分析与回测 | Agent4 |
| `knowledge/策略/含退市股全策略回测报告.md` | 含退市股的全策略回测 | Agent4 |
| `knowledge/LLM-Wiki-工作法.md` | 🏗️ 工作规范 — Karpathy LLM Wiki工作法（非策略，见知识库根目录） | Agent4 |
| `data/策略规则.json` | 买入/卖出策略信号定义 | Agent4 |
| `data/选股规则.json` | 4种选股模式独立权重配置 | Agent5 + Agent4 |
| `data/仓位管理规则.json` | 4种市场环境仓位上限 | Agent3 + Agent4 |
| `data/止损规则.json` | 止损量化触发条件 | Agent3 + Agent4 |
| `knowledge/复盘记录/` | 历史复盘数据（偏差分析、准确率） | Agent4 |

## 风控规则

- 大盘跌破20日均线 → 减仓至5成以下
- 大盘跌破60日均线 → 清仓
- 单票亏损达-7% → 强制止损
- 单票最大仓位：20%（震荡市）
- 总仓位上限：80%（震荡市）/ 100%（牛市确认）

## 定时任务（Claude Code 内置 Cron 管理）

使用 Claude Code 内置的 `CronCreate`/`CronList`/`CronDelete` 工具管理定时任务，
任务持久化存储在 `.claude/scheduled_tasks.json`。

| 时间 | 任务 | cron | 触发方式 |
|------|------|------|---------|
| **18:03 工作日** | **🔄 数据自动同步** | `3 18 * * 1-5` | CronCreate → auto_sync.py → DB更新 |
| 07:00 工作日 | Agent1 情报采集 | `7 7 * * 1-5` | CronCreate → Python脚本 → 微信推送 |
| 07:30 工作日 | Agent8 政策分析 | `33 7 * * 1-5` | CronCreate → Python脚本 → 微信推送 |
| 08:00 工作日 | Agent9 游资追踪 | `3 8 * * 1-5` | CronCreate → Python脚本 → 微信推送 |
| 08:30 工作日 | Agent2 技术分析 | `13 8 * * 1-5` | CronCreate → Python脚本 → 微信推送 |
| 09:00 工作日 | Agent5 早盘选股 | `17 9 * * 1-5` | CronCreate → Python脚本 → 微信推送 |
| 12:00 工作日 | Agent5 午盘选股 | `23 12 * * 1-5` | CronCreate → Python脚本 → 微信推送 |
| 21:00 工作日 | Agent4 复盘 | `37 21 * * 1-5` | CronCreate → Python脚本 → 微信推送 |
| 21:30 工作日 | Agent5 晚间选股 | `47 21 * * 1-5` | CronCreate → Python脚本 → 微信推送 |
| 按需 | Agent3 风控检查 | - | 手动 `/风控官` |
| 按需 | Agent5 盘中/按需选股 | - | 手动 `/选股` 或 `/盘中选股` |
| 按需 | Agent6 操盘手 | - | 手动 `/操盘` |
| 按需 | Agent7 投资领导 | - | 手动 `/决策` |
| 按需 | Agent8 政策分析 | - | 手动 `/政策` |
| 按需 | Agent9 游资追踪 | - | 手动 `/游资` 或 `/龙虎榜` |

> **注意**：cron 分钟字段使用非整点值(7/13/17/23/37/47)以避免:00/:30的集中负载。

> 内置工具: `CronCreate` (创建) / `CronList` (查询) / `CronDelete` (删除)

### 本地命令

| 命令 | 触发Skill | 说明 |
|------|----------|------|
| `/情报员` | agent1-情报员 | 情报采集+报告生成 |
| `/分析师` | agent2-分析师 | 技术分析+板块排名 |
| `/风控官` | agent3-风控官 | 风控检查+止损监控+操盘审查 |
| `/复盘师` | agent4-复盘师 | 复盘+偏差分析+知识库更新 |
| `/选股` | agent5-选股机器人 | 多因子选股（默认晚间模式） |
| `/盘中选股` | agent5-选股机器人 | 盘中异动选股（intraday模式） |
| `/操盘` | agent6-操盘手 | 制定交易计划 |
| `/决策` | agent7-投资领导 | 综合决策+质量审核+冲突仲裁 |
| `/政策` | agent8-政策分析师 | 政策影响分析+宏观解读 |
| `/游资` | agent9-游资追踪师 | 龙虎榜分析+游资追踪+资金情绪 |
| `/问股` | agent-问股 | 自然语言策略问股（均线/缠论/波浪/MACD等9大策略） |

---

## 📱 PushPlus 微信推送（主通知通道）

使用 [PushPlus](https://www.pushplus.plus/) API 推送报告和通知到微信个人号（通过 PushPlus 公众号）。

- **Token** 已配置在 `.claude/settings.local.json` 的 `env.PUSHPLUS_TOKEN`
- **发送脚本**：`python scripts/utils/wechat_send.py` （默认走 PushPlus）
- **推送内容**：Markdown 格式报告原文
- **速率限制**：免费版约 5条/分钟，大报告间隔 3-5秒

> `wechat_send.py` 自动检测 PUSHPLUS_TOKEN，有则走 PushPlus，无则走 cc-connect（ilink）。

## 🚀 cc-connect 微信个人号消息桥接（备选通道）

项目使用 [cc-connect](https://github.com/chenhg5/cc-connect) 对接微信个人号（ilink），实现手机微信 → Claude Code 的双向对话。**注意：cc-connect 目前已无法稳定连接 ilink（gateway error），改用 PushPlus 作为主推送通道。**

### 架构

```
手机微信
    ↓
[微信 ilink 长轮询]
    ↓
      cc-connect 守护进程
        ↓              ↓
   Claude Code      Python 脚本
  (AI对话/自由提问)  (预设命令)
```

### 配置

配置文件：`cc-connect.toml`（项目根目录，**已 .gitignore，含Token**）

```toml
[[projects.platforms]]
type = "weixin"      # 微信个人号（ilink）
```

### 微信设置

```bash
cc-connect weixin setup --config cc-connect.toml --project stock-research
# 用手机微信扫描二维码即可绑定
```

### 启动

cc-connect 已在后台运行中。如需手动启动：

```bash
cc-connect start --config cc-connect.toml
```

---

## Security

- **Token 管理**：Tushare Token 存储在 `.claude/settings.local.json`（已在 `.gitignore` 中排除）
- **环境变量**：所有 Token 通过 settings 的 `env` 字段注入，不在脚本中硬编码
- **Git 清理**：已执行 `git filter-branch` 清除历史中的所有 token 痕迹
- **批处理文件**：`docs/archive/run-agent*.bat` 不包含任何凭证，依赖自动加载的环境变量

## Agent 数据脚本

```bash
# Agent1 - 情报数据采集
source venv/Scripts/activate
python -X utf8 scripts/agent1-情报采集/fetch_all.py [YYYYMMDD]

# Agent2 - 技术分析
source venv/Scripts/activate
python -X utf8 scripts/agent2-技术分析/analyze.py [YYYYMMDD]

# Agent3 - 风控检查（可自动审查操盘手交易计划）
source venv/Scripts/activate
python -X utf8 scripts/agent3-风控/risk_check.py [--env-score N] [--trade-plan data/raw/交易原始数据_YYYYMMDD.json]

# Agent4 - 复盘
source venv/Scripts/activate
python -X utf8 scripts/agent4-复盘/review.py [YYYYMMDD]

# Agent5 - 选股（4种模式：pre_market/intraday/noon/evening）
source venv/Scripts/activate
python -X utf8 scripts/agent5-选股/stock_picker.py [--mode evening] [--top-n 5]

# Agent6 - 交易计划
source venv/Scripts/activate
python -X utf8 scripts/agent6-操盘/trader.py

# Agent7 - 综合决策（含质量审核+打回重做+冲突仲裁）
source venv/Scripts/activate
python -X utf8 scripts/agent7-决策/leader.py
# 脚本自动执行：
#   1. 各Agent质量审核（结构化标准）
#   2. 不合格项生成打回指令（含改进要求）
#   3. 检查已打回的Agent是否重做（#REWORKED标记）
#   4. 风控vs操盘冲突检测与仲裁
#   5. 输出最终投资决策

# Agent-问股 — 自然语言策略问股（9大策略模板）
source venv/Scripts/activate
python -X utf8 scripts/agent_ask/ask.py --code 000001.SZ --strategy 均线
# 可选策略：均线/缠论/波浪/量价/题材/MACD/KDJ/RSI/布林/综合
# --mode detail 输出详细分析

# 📋 Harness 工具（learn-claude-code 设计模式）
# ============================================#

# TodoWrite — 规划工具（s05模式：先列计划再执行）
python scripts/utils/todo_write.py --create "今日任务" --step "步骤1:做A" --step "步骤2:做B"
python scripts/utils/todo_write.py --update 1 --status completed
python scripts/utils/todo_write.py --show

# Task System DAG — 文件持久化任务（s12模式：大目标拆小任务，依赖图）
python scripts/utils/task_manager.py create --subject "任务名" --description "描述" --blocked-by task_id_1 task_id_2
python scripts/utils/task_manager.py claim TASK_ID --owner Agent2
python scripts/utils/task_manager.py update TASK_ID --status completed
python scripts/utils/task_manager.py list
python scripts/utils/task_manager.py dag TASK_ID

# MessageBus — Agent间通信（s15模式：JSONL文件邮箱）
python scripts/utils/message_bus.py send --from Agent1 --to Agent7 --content "消息内容" --type result
python scripts/utils/message_bus.py read Agent7
python scripts/utils/message_bus.py list
```

## 优化历史（Darwin）

| 日期 | 分支 | 轮次 | Δ | 提交数 |
|------|------|------|---|-------|
| 2026-06-27 | `auto-optimize/20260627-0020` | **达尔文1.0** — 全团队D3/D4/D9首轮覆盖 | 平均+12.3→77.4 | 7 commits |
| 2026-06-27 | `auto-optimize/20260627-0020` | **达尔文1.0续** — 五星优化+标准化+test-prompts | 平均+19.7→97.1 (五星) | 17 commits |
| 2026-06-27 | `auto-optimize/20260627-0020` | **达尔文2.0** — 脚本Bug修复+全员D3/D4/D9再升级 | +22D3 +13D4 +16D9 | 脚本Bug修复+全团队升级 |
| 2026-06-27 | `auto-optimize/20260627-0020` | **达尔文3.0** — 全量Python脚本D3/D4/D9代码级嵌入+知识库升级 | +655行D3/D4/D9代码级实现 | 13 files, 7ab7249 |
| 2026-06-27 | `auto-optimize/20260627-0020` | **达尔文4.0** — 全项目审计修复+跨SKILL引用+知识库标准化+QQ MCP+README | 15项修复+7项优化 | 90d520e |
| 2026-07-02 | `auto-optimize/20260627-0020` | **达尔文5.0** — SKILL标准化(22项)+Python覆盖补全(6脚本)+健康度修复(14项)+知识库升级 | +154D3 +81D4 +82D9, 150 try/except | 5 commits (7216601, f2b68ab, 73d0b8f, e470772, 6bbf73f) |
| 2026-07-02 | `auto-optimize/20260627-0020` | **达尔文6.0** — 全项目一致性审计+30项修复(5CRITICAL+12HIGH+13MEDIUM)+Telegram→PushPlus迁移+裸except消除 | 7 SKILL推送标准化 + 6处裸except修复 + 14处硬编码/路径修复 | d9b678a |
| 2026-07-02 | `auto-optimize/20260627-0020` | **达尔文7.0** — 全项目全面审计修复(5CRITICAL+7HIGH+13MEDIUM+11LOW) | CLAUDE.md计数刷新+risk_check总资产Bug+stock_picker死代码+静默pass消除+硬编码env变量化+SKILL标准化+知识库权重修正+基础设施加固 | e3c2ec9 |
| 2026-07-03 | `auto-optimize/20260627-0020` | **达尔文8.0** — debate.py新增+决策审计日志+三级风控委员会+决策记忆注入+TradingAgents借鉴分析 | 4项架构升级 + 知识库 + CLAUDE.md | 无独立提交（合并到达尔文9.0） |
| 2026-07-04 | `auto-optimize/20260627-0020` | **达尔文9.0** — 借鉴ZhuLinsen三项目全面架构升级(8因子体系+L1→L2→L3+LLM排序+问股系统) | L2 LLM排序+l2_rerank+agent_ask问股9策略+SKILL+env | 已完成 |
| 2026-07-04 | `auto-optimize/20260627-0020` | **达尔文10.0** — 全项目全面审计修复101项(27CRITICAL+13HIGH+37MEDIUM+24LOW) | 知识库+data全面审计修复14项+knowledge_lint 2项Bug修复+补录11只指数 | c31188d, 1a83e8f, 26682e1 |
| 2026-07-04 | `auto-optimize/20260627-0020` | **达尔文11.0** — 全项目全面审计修复105项(8CRITICAL+22HIGH+31MEDIUM+40LOW+4INFO) | 7维度并行审计+交叉引用+38项自动修复+requirements.txt+GHA Agent4补全+Token安全+CLAUDE.md修正 | c31188d |
| 2026-07-06 | `auto-optimize/20260627-0020` | **达尔文12.0** — 全项目深度全面优化(3CRITICAL+6HIGH+8MEDIUM+5LOW共22项) | DB层API封装(6个新方法+4索引)+Agent数据访问重构(trader/leader/review DB优先)+裸SQL消除+文档修复+配置同步 | |
| 2026-07-07 | `auto-optimize/20260627-0020` | **达尔文13.0** — Claude Code基础设施升级(全程opus学习+15项改进) | settings.json全面升级(4钩子/扩展权限)+hooks保护(pre-knowledge/session-summary/snapshot)+agents(deep-auditor/code-reviewer/synthesizer)+workflow(health-check)+CLAUDE.md目录结构更新+memory文档化 | |
| 2026-07-07 | `auto-optimize/20260627-0020` | **达尔文13.1** — hooks Node.js→Python修复+MCP移除+learn-claude-code 3模式安装(全程opus学习) | hooks重写(protect-knowledge/save-session-summary/save-snapshot .js→.py); MCP claw/qq移除; PostToolUse auto-lint; agent增强; .claude/rules/; settings优化; learn-claude-code 20章研读+TodoWrite+TaskSystem+MessageBus安装 | |
| 2026-07-07 | `auto-optimize/20260627-0020` | **达尔文14.0** — 全项目深度优化审计(3CRITICAL+15MEDIUM+8LOW共26项) | 数据正确性(ask.py新鲜度校验/fetch_all指数pct_chg/auto_sync覆盖率跳过); DB集中化(11索引注册/INDICATOR_COLS/CREATE TABLE去重); Agent正确性(trader涨跌停689920/hot_money_net_lg/risk_check key+DB单例); 异常处理强化; 知识库去重+INDEX/CHANGES; 10项LOW修复 | **当前** |

优化内容（第9轮-达尔文9.0）：借鉴ZhuLinsen三项目全面架构升级
- **L1→L2→L3选股管线**：stock_picker.py重构为三级管线(L1评分→L2重排序→L3后置分析器)
- **L2 LLM相对排序**：新建 scripts/utils/l2_rerank.py — LiteLLM驱动，支持OpenAI/Claude/DeepSeek，无LLM自动降级规则排序
- **L3 Scorecard后置分析器**：新建 scripts/utils/scorecard.py — 突破确认/量价配合/均线排列/板块动量/基本面5项规则加减分
- **Agent策略问股**：新建 scripts/agent_ask/ask.py + skills/agent-问股/SKILL.md — 9大策略模板(均线/缠论/波浪/MACD/量价/RSI/布林/KDJ/综合)，自然语言问股
- **因子体系升级**：5因子→8因子(新增流动性/稳定性/反转)，非线性评分曲线可配置化(scoring_profile)
- **风险叠加层**：新建 scripts/utils/risk_overlay.py — 独立6项风险检查(涨跌/量比/PE/MACD/PB/连跌)
- **多数据源fallback**：新建 scripts/utils/data_provider.py — Tushare+AkShare自动fallback Provider层
- **零成本部署**：新建 .github/workflows/00-daily-analysis.yml — GitHub Actions定时运行全Agent流水线
- **知识库升级**：knowledge/策略/ZhuLinsen三项目借鉴分析.md + 选股策略.md 8因子体系同步
- **配置升级+修复**：data/选股规则.json 新增8因子权重表 + scoring_profile + 热点板块关键词外部化

优化内容（第7轮-Darwin 7.0）：全项目全面审计修复37项
- **CRITICAL修复**：CLAUDE.md D3/D4/D9计数全部刷新（6个Agent过时）、risk_check.py总资产字段名错误修复、stock_picker.py 策略规则.json死代码清除、build_portfolio硬编码路径env变量化、wechat_send硬编码bot ID配置化
- **HIGH修复**：review.py 3处静默吞异常改为警告输出、stock_picker.py所有15处静默pass改为有信息警告（含ST/停牌检查）、fetch_remaining硬编码代码env变量化、SKILL.md 1处缺Agent6引用+4处缺内联CHECKPOINT、trader.py硬编码仓位改为从仓位管理规则.json读取
- **MEDIUM修复**：选股策略.md增加4模式权重表、交易执行规则.md缺data/前缀、择时策略.md混合链接格式统一、gitattributes添加、gitignore扩增worktrees+trading_calendar、mcp.json路径相对化、env.example扩增可配置变量

优化内容（第1轮）：22条D3 fallback + 4个D4 CHECKPOINT + 21条D9反例 (原有4 Agent)
新增：Agent5-7全套D3/D4/D9 + 全团队D4升级 + 统一标准化格式 + 全团队test-prompts

优化内容（第2轮-Darwin 2.0）：修复5个脚本Bug + 新增22条D3 + 13条D4 + 16条D9 + 3个新功能模块
- **脚本Bug修复**：移动止损逻辑修正(用真实回撤代替当日涨跌幅)、成长因子独立化(不再复制动量)、涨跌停范围检查、板块准确率自动计算、ST/退市过滤
- **新功能模块**：独立成长因子评分(营收增速+净利增速+ROE)、涨跌停价格区间检查、因子表现排行与调整建议
- **数据修复**：portfolio.json 补全所有持仓的当前价/市值/盈亏/仓位
- D3新增：领跌板块排序防错、移动止损真实回撤、涨跌停超限截断、板块预测模糊匹配、行业集中度超限降权
- D4新增：多工具协同、数据质量时效标注、成长因子独立性验证、移动止损回撤计算、涨跌停范围检查、仓位上限来源追溯
- D9新增：成长因子用动量冒充、情绪因子硬编码50、移动止损用当日涨跌幅、板块预测留空不改、价格超出涨跌停范围

优化内容（第3轮-Darwin 3.0）：全量Python脚本D3/D4/D9代码级嵌入 + 知识库标准化
- **脚本层D3嵌入**：所有10个Python脚本添加结构化D3异常处理表（module docstring），覆盖每个数据源/步骤的触发条件→一线修复→仍失败兜底
- **代码级错误处理强化**：tushare_client.py所有API函数try/except兜底、technical_analysis.py逐指标安全计算+NaN防御+边界保护、trader.py JSON解析+总资产字段多级fallback
- **D4 CHECKPOINT代码级**：读文件前检查存在、计算前检查数据长度、输出前检查完整性（文件存在检查、列名验证、行情覆盖率、总资产校验等）
- **D9反例注释**：每个脚本标注常见错误做法、为什么不要做、应该怎么做
- **知识库升级**：选股策略.md/交易执行规则.md/择时策略.md各新增D3表+D4检查点+D9反例
- **Bug修复**：review.py死代码空循环删除、trader.py总资产字段None兼容、load_json JSONDecodeError保护、technical_analysis.py列名缺失/NaN防御

优化内容（第11轮-达尔文11.0）：全项目全面审计修复105项
- **多维度并行审计**：7维度同时审计（SKILL×8、脚本×34、知识库×14、配置×7、CLAUDE.md、WebUI、GHA+Memory）+ 跨文件交叉引用审计
- **安全修复**：移除memory/投研-QQ通知推送.md中暴露的PushPlus Token明文；修复 _audit_output.py 硬编码C:路径
- **基础设施修复**：创建 requirements.txt（之前缺失导致GHA `pip install` 永远失败）；GHA添加Agent4（复盘师）到工作流；GHA修复 `sync_only` 模式（之前所有Agent无条件运行）
- **CLAUDE.md修正**：stocks.db行数~10M→~4100万；补录达尔文10.0到优化历史；新增11个未引用的工具脚本说明
- **代码质量修复**：修复6处 except:pass 静默吞异常；修复2处硬编码日期；修复2处重复关键词；补全6个SKILL.md的 model 字段
- **配置修复**：修复止损规则.json的type名重名（大盘联动止损×2→大盘联动止损/大盘联动清仓）
- **知识库修复**：CHANGES.md路径前缀补全；复盘记录 correct=null→false；knowledge_lint.py stderr AttributeError加注释

### 工具脚本（scripts/utils/ 补充清单 — 共37个脚本）

| 脚本 | 行数 | 用途 |
|:-----|:----:|:-----|
| `auto_sync.py` | 1,664 | 自动同步流水线(13阶段) |
| `backfill_etf_index.py` | 379 | ETF/指数全量历史回填 |
| `build_portfolio.py` | 257 | 持仓组合构建工具 |
| `data_cleaner.py` | 1,062 | 数据审计+清洗+复权工具 |
| `data_provider.py` | 395 | 多数据源统一Provider层 |
| `db_manager.py` | 2,128 | SQLite数据库管理(建表/迁移/查询) |
| `eastmoney_client.py` | 292 | 东方财富板块数据客户端 |
| `eastmoney_get.py` | 326 | 东方财富新闻/数据API(限流网关) |
| `fetch_all_history.py` | 1,104 | 全量历史行情拉取 |
| `fetch_financials.py` | 369 | 财务数据拉取 |
| `fetch_remaining.py` | 268 | 剩余股票数据补齐 |
| `knowledge_lint.py` | 624 | 知识库健康度检查+变更记录 |
| `l2_rerank.py` | 384 | L2 LLM相对排序器 |
| `md_to_docx.py` | 388 | Markdown→Word文档转换 |
| `migrate_to_db.py` | 370 | 数据迁移工具 |
| `mootdx_provider.py` | 323 | mootdx数据源Provider |
| `research_all_strategies.py` | 1,183 | 全策略含退市股回测 |
| `research_ma96_rsi.py` | 602 | MA96+RSI策略研究脚本 |
| `risk_overlay.py` | 289 | 独立6项风险检查层 |
| `rps.py` | 628 | RPS相对强弱排名计算 |
| `scorecard.py` | 326 | L3 Scorecard后置分析器 |
| `sync_sector_moneyflow.py` | 332 | 板块资金流向同步 |
| `technical_analysis.py` | 402 | 技术指标计算(MACD/KDJ/RSI/布林带) |
| `tushare_client.py` | 176 | Tushare Pro API统一客户端 |
| `wechat_send.py` | 628 | 微信推送(PushPlus/cc-connect) |
| `backfill_indicators.py` | ~400 | 技术指标历史回填 |
| `backfill_ths_daily.py` | ~350 | 同花顺概念板块历史回填 |
| `cninfo_sentiment.py` | ~200 | 巨潮资讯舆情/互动易 |
| `eastmoney_plus.py` | ~150 | 东方财富增强数据接口 |
| `limit_up_board.py` | ~300 | 涨停板/打板情绪分析 |
| `tencent_provider.py` | ~200 | 腾讯K线数据源Provider |
| `ths_provider.py` | ~250 | 同花顺数据源Provider（热榜/预测） |
| `llm_config.py` | ~100 | LLM配置管理（L2重排序辅助） |
| `_proxy.py` | ~80 | SOCKS5代理配置 |
| `todo_write.py` | ~150 | 📋 TodoWrite规划工具（Agent执行前先列计划，learn-claude-code s05模式） |
| `task_manager.py` | ~250 | 📋 文件持久化任务系统DAG（依赖图+认领，learn-claude-code s12模式） |
| `message_bus.py` | ~150 | 📋 Agent间JSONL文件邮箱通信（learn-claude-code s15-s16模式） |

