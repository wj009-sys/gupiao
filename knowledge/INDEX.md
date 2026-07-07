# 📚 知识库索引

> 最后更新：2026-07-07
> 维护者：复盘师(Agent4)
> 说明：每次知识库变更后自动更新此索引

---

## 📁 工作规范

| 文件 | 用途 | 维护者 | 依赖 | 最后更新 |
|:----|:-----|:------|:-----|:--------|
| [LLM-Wiki-工作法.md](LLM-Wiki-工作法.md) | 🏗️ Karpathy LLM Wiki 范式 — 编译而非检索的知识管理方法论 | 全团队 | — | 2026-07-03 |

## 📁 元文件

| 文件 | 用途 | 最后更新 |
|:----|:-----|:--------|
| [CHANGES.md](CHANGES.md) | 📋 知识库变更日志，每次更新自动追加 | 2026-07-04 |

## 📁 策略目录

| 文件 | 用途 | 维护者 | 依赖 | 最后更新 |
|:----|:-----|:------|:-----|:--------|
| [选股策略.md](策略/选股策略.md) | 8因子多因子选股权重、筛选参数、4种模式配置、评分曲线配置、风险叠加层 | Agent5 + Agent4 | `data/选股规则.json` | 2026-07-04 |
| [择时策略.md](策略/择时策略.md) | 入场/出场时机、大盘联动、特殊场景择时 | Agent6 + Agent4 | `data/策略规则.json`, `data/止损规则.json`, `data/仓位管理规则.json` | 2026-07-03 |
| [交易执行规则.md](策略/交易执行规则.md) | 买卖规范、仓位分配、止盈止损规则 | Agent6 + Agent4 | `data/仓位管理规则.json`, `skills/agent3-风控官/SKILL.md` | 2026-07-03 |
| [MA96-RSI策略分析.md](策略/MA96-RSI策略分析.md) | MA96+RSI策略回测分析：96源于5分钟K线，日线建议改用MA120 | Agent2+Agent6 | — | 2026-07-04 |
| [含退市股全策略回测报告.md](策略/含退市股全策略回测报告.md) | 全策略(RSI/MACD/KDJ/MABOLL/止盈止损)含327只退市股的幸存者偏差修正回测 | 全员 | — | 2026-07-04 |
| [ZhuLinsen三项目借鉴分析.md](策略/ZhuLinsen三项目借鉴分析.md) | daily_stock_analysis/AlphaSift/AlphaEvo三项深度分析：L1-L2-L3管线/8因子体系/评分曲线/多源fallback/GitHub Actions | 全员 | — | 2026-07-04 |
| [TradingAgents借鉴分析.md](策略/TradingAgents借鉴分析.md) | TradingAgents(TauricResearch)多智能体交易框架架构分析，7项可借鉴改进点分级 | 全员 | — | 2026-07-04 |
| [政策分析策略.md](策略/政策分析策略.md) | 📜 政策分类框架、四类政策影响评估方法、政策→板块映射、持仓关联分析 | Agent8+Agent4 | `data/portfolio.json`, `data/watchlist.json` | 2026-07-05 |
| [游资追踪策略.md](策略/游资追踪策略.md) | 🔥 龙虎榜基础、游资席位识别方法、资金情绪指数计算、与各策略联动 | Agent9+Agent4 | `scripts/utils/eastmoney_get.py` | 2026-07-05 |
| [a-stock-data借鉴分析.md](策略/a-stock-data借鉴分析.md) | 🏗️ a-stock-data技术方案吸收记录：mootdx/em_get()/多级优先级/TradingAgents-astock | 全员 | `scripts/utils/mootdx_provider.py`, `scripts/utils/eastmoney_get.py`, `scripts/utils/data_provider.py` | 2026-07-05 |
| [数据源优先级.md](策略/数据源优先级.md) | 🔌 全局数据源优先级策略(12数据域主/备/兜底+10Agent映射+限流策略) | 全员 | `scripts/utils/_proxy.py`, `scripts/utils/data_provider.py` | 2026-07-05 |

## 📁 复盘记录目录

| 文件 | 日期 | 类型 | 关键内容 |
|:----|:----|:----|:--------|
| [复盘_20260626.json](复盘记录/复盘_20260626.json) | 2026-06-26 | 日常复盘 | — |
| [复盘_20260630.json](复盘记录/复盘_20260630.json) | 2026-06-30 | 日常复盘 | — |
| [复盘_20260702.json](复盘记录/复盘_20260702.json) | 2026-07-02 | 日常复盘 | 大盘-2.03%大阴线复盘 |
| [复盘_20260703.json](复盘记录/复盘_20260703.json) | 2026-07-03 | 日常复盘 | 环境评分分离问题、大面积停牌 |
| [复盘_20260704.json](复盘记录/复盘_20260704.json) | 2026-07-04 | 日常复盘 | 偏空89.5%暂缓交易、风控等级HIGH |
| [复盘_20260706.json](复盘记录/复盘_20260706.json) | 2026-07-06 | 日度复盘 | 全Agent质量审核+风控仲裁+持仓管理 |
| [提取_2026-07-06.md](复盘记录/提取_2026-07-06.md) | 2026-07-06 | 自动知识提取（MD） | 25条知识(17风控+6策略+2市场) |
| [提取_2026-07-06.json](复盘记录/提取_2026-07-06.json) | 2026-07-06 | 自动知识提取（JSON） | 结构化提取数据 |

## 📁 外部引用

| 外部文件 | 关联策略 | 说明 |
|:--------|:---------|:-----|
| `data/策略规则.json` | 择时策略、选股策略 | 买入/卖出策略信号定义 |
| `data/选股规则.json` | 选股策略 | 8因子4种选股模式权重配置 + scoring_profile评分曲线 |
| `data/仓位管理规则.json` | 交易执行规则、择时策略 | 4种市场环境仓位上限 + 3种风控风格 |
| `data/止损规则.json` | 交易执行规则、择时策略 | 止损量化触发条件 + 3种风控风格 |
| `data/trading_calendar.json` | 全部 | 交易日历缓存（1574个交易日） |
| `data/watchlist.json` | 选股策略 | 自选股列表 |
| `data/portfolio.json` | 交易执行规则 | 持仓组合（仓位/市值/盈亏） |
| `CLAUDE.md` | 全部 | Schema配置文件 — 见 [[LLM-Wiki-工作法]] |
| `memory/决策反思.md` | 全部 | 投资领导决策反思，Agent7次日自动加载 |
| `scripts/utils/l2_rerank.py` | 选股策略 | L2 LLM相对排序引擎 |
| `scripts/utils/scorecard.py` | 选股策略 | L3 Scorecard后置分析器 |
| `scripts/utils/risk_overlay.py` | 选股策略 | 风险叠加层(6项独立惩罚) |
| `scripts/utils/data_provider.py` | 全部 | Tushare+AkShare多数据源fallback |
| `scripts/agent_ask/ask.py` | 全部 | Agent策略问股(9策略模板) |
| `skills/agent1-情报员/SKILL.md` | 全部 | 情报采集规范 |
| `skills/agent2-分析师/SKILL.md` | 全部 | 技术分析规范 |
| `skills/agent3-风控官/SKILL.md` | 交易执行规则 | 风控审查标准 |
| `skills/agent4-复盘师/SKILL.md` | 全部 | 复盘师规范 |
| `skills/agent5-选股机器人/SKILL.md` | 选股策略 | 选股机器人操作规范 |
| `skills/agent6-操盘手/SKILL.md` | 交易执行规则 | 操盘手操作规范 |
| `skills/agent7-投资领导/SKILL.md` | 全部 | 投资领导规范 |
| `skills/agent-问股/SKILL.md` | 全部 | 策略问股Agent规范 |
| `skills/agent8-政策分析师/SKILL.md` | 政策分析策略 | 政策分析师操作规范 |
| `skills/agent9-游资追踪师/SKILL.md` | 游资追踪策略 | 游资追踪师操作规范 |
| `scripts/utils/message_bus.py` | 全部 | Agent团队文件收件箱通信总线（Phase1） |
| `scripts/utils/llm_client.py` | 全部 | LLM API三层错误恢复封装（Phase1） |
| `scripts/utils/context_compact.py` | 全部 | 四层上下文压缩管线（Phase1） |
| `scripts/utils/agent_orchestrator.py` | 全部 | Agent波次式并行调度器（Phase2） |
| `scripts/utils/memory_extractor.py` | 全部 | 知识自动提取器（Phase3） |
| `scripts/run_daily_pipeline.py` | 全部 | 每日全流程管线入口（Phase2+3） |
| `scripts/utils/eastmoney_get.py` | 全部 | 东方财富限流数据网关（吸收 a-stock-data） |
| `scripts/utils/mootdx_provider.py` | 全部 | mootdx TCP免费数据源（吸收 a-stock-data） |
| `knowledge/复盘记录/复盘_20260706.json` | 全部 | 2026-07-06 日度复盘数据 |

## 🔄 双向引用关系

```
选股策略.md ←→ data/选股规则.json
     ↕ (因子调整建议)     scripts/utils/l2_rerank.py
     ↕                    scripts/utils/scorecard.py
复盘记录/                 scripts/utils/risk_overlay.py
     ↕ (偏差分析)
     ↕ (知识库更新)   择时策略.md ←→ data/策略规则.json
                                    data/止损规则.json
交易执行规则.md ←→ data/仓位管理规则.json
     ↕            skills/agent3-风控官/SKILL.md
     ↕            skills/agent6-操盘手/SKILL.md

ZhuLinsen三项目借鉴分析.md ←→ 选股策略.md (8因子/评分曲线/管线重构)
     ↕                         择时策略.md (第三方风控)
     ↕                         交易执行规则.md (L3 Scorecard)
TradingAgents借鉴分析.md ←→ 全部 (多Agent制衡/决策记忆/三方辩论/审计日志)
     ↕
memory/决策反思.md ←─ Agent7次日自动加载 (决策记忆)
```
| [复盘记录/提取_2026-07-07.json](复盘记录/提取_2026-07-07.json) | 未知日期 | 日常复盘 | — |
| [复盘记录/提取_2026-07-07.md](复盘记录/提取_2026-07-07.md) | 未知日期 | 日常复盘 | — |

