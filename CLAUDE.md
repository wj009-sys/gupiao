# 股票投研自动化项目

## 项目目标

构建A股自动化投研团队，包含7个AI Agent角色，每天自动完成情报采集→技术分析→选股推荐→风控检查→交易计划→复盘迭代的完整闭环，由投资领导统筹管理。每个Agent都经过Darwin Skill优化（评分从平均65.1提升至77.4）。

## Agent 团队

| Agent | Skill | 脚本 | 达成分数 | 定时 |
|-------|-------|------|---------|------|
| 🕵️ Agent1 情报员 | `skills/agent1-情报员/SKILL.md` | `fetch_all.py` | **96** (Darwin五星) | 07:00 |
| 📊 Agent2 分析师 | `skills/agent2-分析师/SKILL.md` | `analyze.py` | **97** (Darwin五星) | 08:30 |
| 🛡️ Agent3 风控官 | `skills/agent3-风控官/SKILL.md` | `risk_check.py` | **97** (Darwin五星) | 按需 |
| 🔄 Agent4 复盘师 | `skills/agent4-复盘师/SKILL.md` | `review.py` | **97** (Darwin五星) | 21:00 |
| 🔍 Agent5 选股机器人 | `skills/agent5-选股机器人/SKILL.md` | `stock_picker.py` | **100** (Darwin五星) | 按需 |
| 🎯 Agent6 操盘手 | `skills/agent6-操盘手/SKILL.md` | `trader.py` | **97** (Darwin五星) | 按需 |
| 🏆 Agent7 投资领导 | `skills/agent7-投资领导/SKILL.md` | `leader.py` | **96** (Darwin五星) | 按需 |

### 优化后新增通用模块（所有Skill均含）

| 模块 | 说明 | 来源 |
|-----|------|------|
| 🚨 异常处理表 | 每个数据源/步骤的「触发条件→一线修复→仍失败兜底」三段式 | Darwin D3 |
| 🔴 CHECKPOINT | 关键决策前的数据完整性/结论可验证性检查点 | Darwin D4 |
| ⛔ 工作反例 | 「不要这样做」的黑名单清单（含为什么+应该怎么做） | Darwin D9 |

### Agent1：情报员（信息采集）
- **职责**：每天早7点自动抓取全网财经资讯（政策、公告、龙虎榜、资金流向）
- **输出**：`reports/日报/情报/情报摘要_YYYY-MM-DD.md`
- **工具**：WebFetch + Tavily Search + Tushare
- **D3异常**：7条fallback（数据源全挂、API限频、报告写入失败等）
- **D4检查**：报告生成前数据完整性检查
- **D9反例**：禁止堆砌新闻、传播传闻、忽略持仓、使用过期信息

### Agent2：分析师（技术分析）
- **职责**：基于情报拉取行情，计算MACD/KDJ/RSI/布林带，板块强度排名
- **输出**：`reports/日报/分析/分析报告_YYYY-MM-DD.md`
- **指标参数**：MACD(12,26,9), KDJ(9,3,3), RSI(14), BOLL(20)
- **D3异常**：5条fallback（脚本报错、板块数据空、指标计算失败等）
- **D4检查**：强烈信号必须满足技术+消息双重条件
- **D9反例**：禁止模糊信号、编造数据、矛盾建议、忽视成交量

### Agent3：风控官（风险管理 + 操盘审查）
- **职责**：大盘环境评估、止损线检查、仓位超限监控、**审查操盘手交易计划（买入/卖出清单风控审批）**、**独立持仓加减仓建议**、**与操盘手分歧时上报投资领导仲裁**
- **输出**：风控提醒（`reports/日报/风控/`）+ 操盘计划审查章节 + 冲突项汇总
- **规则文件**：`data/仓位管理规则.json`, `data/止损规则.json`
- **D3异常**：8条fallback（新增操盘计划缺失、冲突无法自动解决等）
- **D4检查**：9个CP（新增操盘计划审查、买入止损校验、冲突上报等）
- **D9反例**：8条（新增盲从操盘手、否决无理由、分歧不上报等）

### Agent4：复盘师（自我进化）
- **职责**：每晚9点复盘当天研判，对比预测vs实际，更新知识库
- **输出**：`reports/日报/复盘/复盘报告_YYYY-MM-DD.md`
- **维护**：`knowledge/策略/` + `knowledge/复盘记录/`
- **D3异常**：5条fallback（脚本报错、无分析报告、首次复盘等）
- **D4检查**：每个偏差必须附带改进措施；知识库必须实际更新
- **D9反例**：禁止只报喜不报忧、流于表面的偏差分析、改框架过频

### Agent5：选股机器人（多因子选股）
- **职责**：基于情报热点+技术面+基本面因子，筛选候选股票池。支持4种选股模式：早盘/盘中/午盘/晚间
- **输出**：`reports/日报/选股/选股建议_YYYY-MM-DD.md`（各模式不同格式）
- **脚本参数**：`python stock_picker.py --mode pre_market|intraday|noon|evening --top-n 5`
- **定时**：09:00早盘 / 12:00午盘 / 21:00晚间选股；盘中按需手动触发
- **配置**：`data/选股规则.json`（含4种模式独立权重） + `knowledge/策略/选股策略.md`
- **D3异常**：13条fallback（新增模式选择错误、停复牌失败、盘中数据不可用等）
- **D4检查**：8个CP（新增模式匹配检查、模式特有检查）
- **D9反例**：11条（新增早盘用晚间权重、盘中看估值、不看复盘偏差等）

### Agent6：操盘手（交易计划）
- **职责**：基于选股建议+风控约束+仓位规则，制定交易计划，**接受风控官审查并按意见修改**
- **输出**：`reports/日报/操盘/交易计划_YYYY-MM-DD.md`
- **配置**：`data/仓位管理规则.json` + `knowledge/策略/交易执行规则.md`
- **D3异常**：5条fallback（无风控报告、无选股建议、行情缺失等）
- **D4检查**：买入合规、止损必设、总仓位上限、优先级别注
- **D9反例**：禁止全仓一只、频繁交易、逆势加仓、忽视风控
- **制衡机制**：交易计划由风控官（Agent3）审查，分歧由投资领导（Agent7）仲裁

### Agent7：投资领导（团队管理 + 质量审核 + 冲突仲裁）
- **职责**：统筹调度所有Agent，分工派活、**结构化审核每个Agent输出质量（不合格打回重做）**、**仲裁风控官vs操盘手冲突**、最终决策
- **输出**：`reports/日报/决策/投资决策_YYYY-MM-DD.md`（含质量审核章节、打回重做指令、冲突仲裁章节）
- **管理对象**：Agent1-6全部归属投资领导调度
- **质量审核**：每Agent专属审核标准（情报员6项、分析师5项、选股机器人5项、风控官5项、操盘手5项），含必须项和建议项
- **打回重做**：不合格报告附带具体改进要求，重做后通过 `#REWORKED` 标记验证
- **仲裁机制**：风控官否决操盘手交易时，投资领导逐项裁定（风控一票否决/折中部分止盈/有条件放行）
- **D3异常**：11条fallback（新增打回后未重做、打回指令无法送达等）
- **D4检查**：11个CP（新增质量审核、不合格打回、重做跟踪等）
- **D9反例**：14条（新增从不打回、不给具体要求、不跟踪重做、因时间降低标准放行等）

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

## 目录结构

```
.mcp.json            - MCP 服务器配置（claw 定时调度）
.env.example         - 环境变量模板
构想.md              - 项目初始构想文档
投研团队设计方案.md   - 投研团队详细设计方案
data/              - 数据文件（持仓、自选、规则配置）
  ├── raw/          - 原始数据缓存（gitignored）
reports/           - 报告输出（日报/周报/月报，日报文件已 gitignored）
  ├── 日报/情报/   - Agent1 情报摘要
  ├── 日报/分析/   - Agent2 分析报告
  ├── 日报/选股/   - Agent5 选股建议
  ├── 日报/风控/   - Agent3 风控报告
  ├── 日报/操盘/   - Agent6 交易计划
  ├── 日报/决策/   - Agent7 投资决策
  ├── 日报/复盘/   - Agent4 复盘报告
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
  └── utils/       - 工具函数（Tushare客户端、技术指标库）
.claude/           - Claude 配置
  ├── mcp-servers/
  │   └── claw/        - 定时调度 MCP 服务器 (cron)
  ├── settings.local.json - 本地凭据与Token（gitignored）
  └── scheduled_tasks.json - 定时任务存储
knowledge/         - 知识库（Agent4 维护更新）
  ├── 策略/        - 选股/择时/交易策略
  └── 复盘记录/    - 历史复盘
memory/            - Claude 持久记忆
skills/            - 自定义 Skills
  ├── agent1-情报员/SKILL.md + test-prompts.json
  ├── agent2-分析师/SKILL.md + test-prompts.json
  ├── agent3-风控官/SKILL.md + test-prompts.json
  ├── agent4-复盘师/SKILL.md + test-prompts.json
  ├── agent5-选股机器人/SKILL.md
  ├── agent6-操盘手/SKILL.md
  └── agent7-投资领导/SKILL.md
```

## 数据源

- **Tushare Pro**：A股行情、财务、龙虎榜、资金流向（token通过 `.claude/settings.local.json` 自动加载，不硬编码）
- **网页抓取**：财联社、东方财富、巨潮资讯

## 风控规则

- 大盘跌破20日均线 → 减仓至5成以下
- 大盘跌破60日均线 → 清仓
- 单票亏损达-7% → 强制止损
- 单票最大仓位：20%（震荡市）
- 总仓位上限：80%（震荡市）/ 100%（牛市确认）

## 定时任务（claw MCP 托管）

使用 claw MCP 服务器管理定时任务，存储在 `.claude/scheduled_tasks.json`。

| 时间 | 任务 | cron | 触发方式 |
|------|------|------|---------|
| 07:00 工作日 | Agent1 情报采集 | `0 7 * * 1-5` | claw MCP + Python脚本 → 微信推送 |
| 08:30 工作日 | Agent2 技术分析 | `30 8 * * 1-5` | claw MCP + Python脚本 → 微信推送 |
| 09:00 工作日 | Agent5 早盘选股 | `0 9 * * 1-5` | claw MCP + Python脚本 → 微信推送 |
| 12:00 工作日 | Agent5 午盘选股 | `0 12 * * 1-5` | claw MCP + Python脚本 → 微信推送 |
| 21:00 工作日 | Agent4 复盘 + Agent5 晚间选股 | `0 21 * * 1-5` | claw MCP + Python脚本 → 微信推送 |
| 按需 | Agent3 风控检查 | - | 手动 `/风控官` |
| 按需 | Agent5 盘中选股/按需选股 | - | 手动 `/选股` 或 `/盘中选股` |
| 按需 | Agent6 操盘手 | - | 手动 `/操盘` |
| 按需 | Agent7 投资领导 | - | 手动 `/决策` |

> MCP server: `.claude/mcp-servers/claw/server.js` (stdio JSON-RPC)
> 工具: `mcp__claw__cron` (创建) / `cron_list` (查询) / `cron_delete` (删除)

### 本地命令

| 命令 | 触发Skill | 说明 |
|------|----------|------|
| `/情报员` | agent1-情报员 | 情报采集+报告生成 |
| `/分析师` | agent2-分析师 | 技术分析+板块排名 |
| `/风控官` | agent3-风控官 | 风控检查+止损监控 |
| `/复盘师` | agent4-复盘师 | 复盘+偏差分析+知识库更新 |

---

## 🚀 cc-connect 微信个人号消息桥接

项目使用 [cc-connect](https://github.com/chenhg5/cc-connect) 对接微信个人号（ilink），实现手机微信 → Claude Code 的双向对话。

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
- **批处理文件**：`run-agent*.bat` 不包含任何凭证，依赖自动加载的环境变量

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

# Agent5 - 选股
source venv/Scripts/activate
python -X utf8 scripts/agent5-选股/stock_picker.py [--top-n 5]

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
```

## 优化历史（Darwin）

| 日期 | 分支 | 平均分 | Δ | 提交数 |
|------|------|-------|---|-------|
| 2026-06-27 | `auto-optimize/20260627-0020` | **77.4** | +12.3 | 7 commits, 0 revert |
| 2026-06-27 | `auto-optimize/20260627-0020` | **97.1** | +19.7 (Darwin五星) | 17 commits, 0 revert |
| 2026-06-27 | `auto-optimize/20260627-0020` | **达尔文2.0** | +22D3 +13D4 +16D9 | 脚本Bug修复+全团队D3/D4/D9再升级 |

优化内容（第1轮）：22条D3 fallback + 4个D4 CHECKPOINT + 21条D9反例 (原有4 Agent)
新增：Agent5-7全套D3/D4/D9 + 全团队D4升级 + 统一标准化格式 + 全团队test-prompts

优化内容（第2轮-Darwin 2.0）：修复5个脚本Bug + 新增22条D3 + 13条D4 + 16条D9 + 3个新功能模块
- **脚本Bug修复**：移动止损逻辑修正(用真实回撤代替当日涨跌幅)、成长因子独立化(不再复制动量)、涨跌停范围检查、板块准确率自动计算、ST/退市过滤
- **新功能模块**：独立成长因子评分(营收增速+净利增速+ROE)、涨跌停价格区间检查、因子表现排行与调整建议
- **数据修复**：portfolio.json 补全所有持仓的当前价/市值/盈亏/仓位
- D3新增：领跌板块排序防错、移动止损真实回撤、涨跌停超限截断、板块预测模糊匹配、行业集中度超限降权
- D4新增：多工具协同、数据质量时效标注、成长因子独立性验证、移动止损回撤计算、涨跌停范围检查、仓位上限来源追溯
- D9新增：成长因子用动量冒充、情绪因子硬编码50、移动止损用当日涨跌幅、板块预测留空不改、价格超出涨跌停范围
