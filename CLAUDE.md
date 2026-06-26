# 股票投研自动化项目

## 项目目标

构建A股自动化投研团队，包含4个AI Agent角色，每天自动完成情报采集→技术分析→风控检查→复盘迭代的完整闭环。每个Agent都经过Darwin Skill优化（评分从平均65.1提升至77.4）。

## Agent 团队

| Agent | Skill | 脚本 | 达成分数 | 定时 |
|-------|-------|------|---------|------|
| 🕵️ Agent1 情报员 | `skills/agent1-情报员/SKILL.md` | `fetch_all.py` | **73.9** (+13.2) | 07:00 |
| 📊 Agent2 分析师 | `skills/agent2-分析师/SKILL.md` | `analyze.py` | **79.1** (+12.0) | 08:30 |
| 🛡️ Agent3 风控官 | `skills/agent3-风控官/SKILL.md` | `risk_check.py` | **76.2** (+12.6) | 按需 |
| 🔄 Agent4 复盘师 | `skills/agent4-复盘师/SKILL.md` | `review.py` | **80.3** (+11.4) | 21:00 |

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

### Agent3：风控官（风险管理）
- **职责**：大盘环境评估、止损线检查、仓位超限监控
- **输出**：风控提醒（`reports/日报/风控/`）
- **规则文件**：`data/仓位管理规则.json`, `data/止损规则.json`
- **D3异常**：5条fallback（脚本报错、行情获取失败、持仓为空等）
- **D4检查**：HIGH风险必须有具体操作步骤（卖什么、卖多少、什么价格）
- **D9反例**：禁止模糊建议、用测试数据当真、忽略大盘、等反弹

### Agent4：复盘师（自我进化）
- **职责**：每晚9点复盘当天研判，对比预测vs实际，更新知识库
- **输出**：`reports/日报/复盘/复盘报告_YYYY-MM-DD.md`
- **维护**：`knowledge/策略/` + `knowledge/复盘记录/`
- **D3异常**：5条fallback（脚本报错、无分析报告、首次复盘等）
- **D4检查**：每个偏差必须附带改进措施；知识库必须实际更新
- **D9反例**：禁止只报喜不报忧、流于表面的偏差分析、改框架过频

## 目录结构

```
data/              - 数据文件（持仓、自选、规则配置）
reports/           - 报告输出
  ├── 日报/情报/   - Agent1 情报摘要
  ├── 日报/分析/   - Agent2 分析报告
  ├── 日报/风控/   - Agent3 风控报告
  ├── 日报/复盘/   - Agent4 复盘报告
  ├── 周报/        - 每周汇总
  └── 月报/        - 每月汇总
scripts/           - Python 分析脚本
  ├── agent1-情报采集/
  ├── agent2-技术分析/
  ├── agent3-风控/
  ├── agent4-复盘/
  └── utils/       - 工具函数（Tushare客户端、技术指标库）
knowledge/         - 知识库（Agent4 维护更新）
  ├── 策略/        - 选股/择时策略
  └── 复盘记录/    - 历史复盘
memory/            - Claude 持久记忆
skills/            - 自定义 Skills
  ├── agent1-情报员/SKILL.md + test-prompts.json
  ├── agent2-分析师/SKILL.md + test-prompts.json
  ├── agent3-风控官/SKILL.md + test-prompts.json
  └── agent4-复盘师/SKILL.md + test-prompts.json
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
| 07:00 工作日 | Agent1 情报采集 | `0 7 * * 1-5` | claw MCP + Python脚本 |
| 08:30 工作日 | Agent2 技术分析 | `30 8 * * 1-5` | claw MCP + Python脚本 |
| 21:00 工作日 | Agent4 复盘分析 | `0 21 * * 1-5` | claw MCP + Python脚本 |
| 按需 | Agent3 风控检查 | - | 手动 `/风控官` |

> MCP server: `.claude/mcp-servers/claw/server.js` (stdio JSON-RPC)
> 工具: `mcp__claw__cron` (创建) / `cron_list` (查询) / `cron_delete` (删除)

## 使用方式

| 命令 | 触发Skill | 说明 |
|------|----------|------|
| `/情报员` | agent1-情报员 | 情报采集+报告生成 |
| `/分析师` | agent2-分析师 | 技术分析+板块排名 |
| `/风控官` | agent3-风控官 | 风控检查+止损监控 |
| `/复盘师` | agent4-复盘师 | 复盘+偏差分析+知识库更新 |

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

# Agent3 - 风控检查
source venv/Scripts/activate
python -X utf8 scripts/agent3-风控/risk_check.py [--env-score N]

# Agent4 - 复盘
source venv/Scripts/activate
python -X utf8 scripts/agent4-复盘/review.py [YYYYMMDD]
```

## 优化历史（Darwin）

| 日期 | 分支 | 平均分 | Δ | 提交数 |
|------|------|-------|---|-------|
| 2026-06-27 | `auto-optimize/20260627-0020` | **77.4** | +12.3 | 7 commits, 0 revert |

优化内容：22条D3 fallback + 4个D4 CHECKPOINT + 21条D9反例
