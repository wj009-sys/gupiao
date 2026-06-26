# 股票投研自动化项目

## 项目目标
构建A股自动化投研团队，包含4个AI Agent角色，每天自动完成情报采集→技术分析→风控检查→复盘迭代的完整闭环。

## Agent 团队

### Agent1：情报员（信息采集）
- **职责**：每天早7点自动抓取全网财经资讯（政策、公告、龙虎榜、资金流向）
- **输出**：reports/日报/情报/ 下的 Markdown 摘要
- **工具**：WebFetch + Tavily Search + tushare-data skill

### Agent2：分析师（技术分析）
- **职责**：基于情报拉取实时行情，计算MACD/KDJ/RSI/布林带，板块强度排名
- **输出**：reports/日报/分析/ 下的分析报告
- **工具**：tushare-data + Python scripts/agent2-技术分析/ + skills/agent2-分析师/SKILL.md
- **指标参数**：MACD(12,26,9), KDJ(9,3,3), RSI(14), BOLL(20)

### Agent3：风控官（风险管理）
- **职责**：大盘环境评估、止损线检查、仓位超限监控
- **输出**：风控提醒（盘中按需触发）
- **规则文件**：data/仓位管理规则.json, data/止损规则.json

### Agent4：复盘师（自我进化）
- **职责**：每晚9点复盘当天研判，对比预测vs实际，计算准确率，更新知识库
- **输出**：reports/日报/复盘/ 下的复盘报告
- **维护**：knowledge/ 下的策略和复盘记录

## 目录结构
```
data/              - 数据文件（持仓、自选、规则配置）
reports/           - 所有报告输出
  ├── 日报/情报/   - Agent1 情报摘要
  ├── 日报/分析/   - Agent2 分析报告
  ├── 日报/复盘/   - Agent4 复盘报告
  ├── 周报/        - 每周汇总
  └── 月报/        - 每月汇总
scripts/           - Python 分析脚本
  ├── agent1-情报采集/
  ├── agent2-技术分析/
  ├── agent3-风控/
  ├── agent4-复盘/
  └── utils/       - 工具函数（Tushare客户端等）
knowledge/         - 知识库（Agent4 维护更新）
  ├── 策略/        - 选股/择时策略
  └── 复盘记录/    - 历史复盘
memory/            - Claude 持久记忆
skills/            - 自定义 Skills（可选）
```

## 数据源
- **Tushare Pro**：A股行情、财务、龙虎榜、资金流向（token 配置于环境变量）
- **网页抓取**：财联社、东方财富、巨潮资讯

## 风控规则（初始）
- 大盘跌破20日均线 → 减仓至5成以下
- 大盘跌破60日均线 → 清仓
- 单票亏损达-7% → 强制止损
- 单票最大仓位：20%
- 总仓位上限：80%（震荡市）/ 满仓（牛市确认）

## 定时任务（claw MCP 托管）

使用自定义 claw MCP 服务器管理定时任务，任务持久化存储在 `.claude/scheduled_tasks.json`。

| 时间 | 任务 | 工具调用 |
|------|------|---------|
| 07:00 工作日 | Agent1 情报采集 | `mcp__claw__cron` |
| 08:30 工作日 | Agent2 技术分析 | `mcp__claw__cron` |
| 21:00 工作日 | Agent4 复盘分析 | `mcp__claw__cron` |
| 按需 | Agent3 风控检查 | 手动 `/风控官` |

> 任务通过 claw MCP server 管理，调用 `mcp__claw__cron` 可创建/查询/删除任务。
> 跟我说「列出所有定时」可查看当前任务列表。

## 使用方式
- `/情报员` 或 `/agent1` — 手动触发情报采集（读取 skills/agent1-情报员/SKILL.md）
- `/agent2` — 手动触发技术分析
- `/agent3` — 手动触发风控检查
- `/agent4` — 手动触发复盘
- `/投研日报` — 查看今日完整投研日报（包含情报+分析+复盘）

## Skills（自定义）

项目自定义 Skills 存储在 `skills/` 目录：

```
skills/
├── agent1-情报员/
│   ├── SKILL.md    ← Agent1 情报采集 Skill 定义
│   └── scripts/
├── agent2-分析师/
│   └── SKILL.md    ← Agent2 技术分析 Skill 定义
├── agent3-风控官/
│   └── SKILL.md    ← Agent3 风控管理 Skill 定义
└── agent4-复盘师/
    └── SKILL.md    ← Agent4 复盘进化 Skill 定义
```

- `/情报员` 或提及「情报」「早报」→ 加载 Agent1
- `/分析师` 或提及「分析」「技术面」「板块排名」→ 加载 Agent2
- `/风控官` 或提及「风控」「风险」「止损」「仓位」→ 加载 Agent3
- `/复盘师` 或提及「复盘」「回顾」「准确率」→ 加载 Agent4

## Agent 数据脚本

```bash
# Agent1 - 情报采集
source venv/Scripts/activate
python -X utf8 scripts/agent1-情报采集/fetch_all.py [YYYYMMDD]

# Agent2 - 技术分析
source venv/Scripts/activate
python -X utf8 scripts/agent2-技术分析/analyze.py [YYYYMMDD]
```

采集结果自动保存到 `data/raw/` 目录，由对应的 Skill 整理为 Markdown 报告。
