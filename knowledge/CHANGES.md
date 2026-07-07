# 📋 知识库变更日志

> 每次知识库更新自动追加，由复盘师(Agent4)维护。
> 格式：`YYYY-MM-DD | 类型 | 文件 | 变更摘要`

## 2026年

### 7月

| 2026-07-05 | feat | scripts/utils/auto_sync.py | 达尔文16.0: moneyflow_hsgt/stock同步阶段(Phase 10/11) + argparse |
| 2026-07-05 | feat | scripts/utils/db_manager.py | 达尔文16.0: upsert_moneyflow_stock/mkt + buy_elg_amount列迁移 |
| 2026-07-05 | fix | scripts/utils/_proxy.py | 达尔文16.0: SOCKS代理不可用自动降级直连 |
| 2026-07-05 | fix | scripts/utils/fetch_*.py | 达尔文16.0: 移除无效revenue_yoy字段引用 |
| 2026-07-05 | feat | webui/ | 达尔文16.0: WEBUI升级(Agent8/9+数据库监控+版本16.0) |
| 2026-07-05 | chore | .claude/scheduled_tasks.json | 达尔文16.0: 自动同步任务ID重命名 |
| 日期 | 类型 | 文件 | 变更摘要 |
|:----|:----|:-----|:--------|
| 2026-07-07 | 📝 upgrade | skills/*/SKILL.md (10 files) | 达尔文13.1技能同步：qq_agent_notify死引用删除(9技能)+TodoWrite第零步(10技能)+Harness工具表(10技能)+MessageBus关联(10技能)+Agent4 research-synthesizer子Agent引用+Agent7 task_manager+deep-auditor+code-reviewer子Agent引用 |
| 2026-07-07 | 📝 feat | scripts/utils/todo_write.py | 达尔文13.1: learn-claude-code s05 TodoWrite规划工具安装 |
| 2026-07-07 | 📝 feat | scripts/utils/task_manager.py | 达尔文13.1: learn-claude-code s12 Task System DAG安装 |
| 2026-07-07 | 📝 feat | scripts/utils/message_bus.py | 达尔文13.1: learn-claude-code s15-s16 MessageBus Agent通信安装 |
| 2026-07-07 | 📝 feat | knowledge/策略/learn-claude-code借鉴分析.md | 达尔文13.1: learn-claude-code 20章对照分析报告 |
| 2026-07-07 | 📝 feat | .gitignore | 达尔文13.1: 添加.tasks/和.current_todos.json |
| 2026-07-07 | 📝 feat | CLAUDE.md | 达尔文13.1: 添加Harness工具章节+工具脚本清单更新(34→37)+优化历史 |
| 2026-07-07 | 🐛 fix | .claude/hooks/*.py | 达尔文13.1: Node.js→Python hooks重写(3个) — 环境中无node导致所有hook静默失败 |
| 2026-07-07 | 🐛 fix | .mcp.json | 达尔文13.1: 移除claw+qq MCP服务器(node依赖, 环境中不可用) — 功能已由内置CronCreate+wechat_send.py覆盖 |
| 2026-07-07 | 🐛 fix | CLAUDE.md | 达尔文13.1: 更新目录结构(.js→.py, 移除mcp-servers子条目); 更新定时任务章节(claw MCP→内置CronCreate) |
| 2026-07-07 | 🐛 fix | .env.example | 达尔文13.1: 移除QQ MCP引用 |
| 2026-07-07 | 📝 feat | .claude/hooks/auto-lint.py | 达尔文13.1: 新增PostToolUse hook — 编辑后自动检查知识库/报告格式 |
| 2026-07-07 | 📝 feat | .claude/settings.json | 达尔文13.1: 添加PostToolUse hook + modelOverrides + cleanupPeriodDays + smallModel |
| 2026-07-07 | 📝 feat | .claude/rules/ | 达尔文13.1: 新增路径作用域规则(agent-scripts/knowledge-files/database-access) |
| 2026-07-07 | 📝 feat | .claude/agents/*.md | 达尔文13.1: 增强Agent定义(isolation/effort/color/background字段) |
| 2026-07-07 | 🗑️ chore | .claude/workflows/darwin-11-audit.js | 达尔文13.1: 归档过时workflow到docs/archive/ |
| 2026-07-07 | 📝 fix | GHA 00-daily-analysis.yml | Python版本3.10→3.12 |
| 2026-07-07 | 📝 fix | CLAUDE.md | 达尔文13.0: 目录结构更新(.claude子目录展开+memory路径澄清) |
| 2026-07-07 | 📝 feat | .claude/workflows/darwin-13-health-check.js | 达尔文13.0: 创建健康检查工作流(settings/hooks/agents审计+知识库检查) |
| 2026-07-07 | 📝 feat | .claude/agents/ | 达尔文13.0: 创建agents目录(deep-auditor/code-reviewer/research-synthesizer) |
| 2026-07-07 | 📝 feat | .claude/hooks/ | 达尔文13.0: 创建hooks目录(保护知识库+会话摘要+压缩快照 3脚本) |
| 2026-07-07 | 📝 feat | .claude/settings.json | 达尔文13.0: settings全面升级(4钩子+扩展权限+effortLevel) |
| 2026-07-07 | 📝 提取 | memory_extractor_2026-07-07 | 自动提取15条知识(4份报告) |
| 2026-07-07 | 📝 提取 | memory_extractor_2026-07-07 | 自动提取10条知识(4份报告) |
| 2026-07-07 | 📝 模块 | Phase3记忆提取器 | MemoryExtractor+管线闭环+集成测试6/6通过+验证已提取6份报告知识 |
| 2026-07-07 | 📝 提取 | memory_extractor_2026-07-06 | 自动提取25条知识(8份报告) |
| 2026-07-07 | 📝 提取 | memory_extractor_2026-07-06 | 自动提取25条知识(8份报告) |
| 2026-07-07 | 📝 模块 | Phase2调度器+管线 | AgentOrchestrator波次调度+run_daily_pipeline入口+集成测试10/10通过 |
| 2026-07-07 | 📝 模块 | Phase1三件套 | MessageBus+LLMClient+ContextCompact+集成测试+.gitignore更新 |
| 2026-07-07 | 📝 决策 | 投资决策_2026-07-07.md | 投资领导补跑7月7日早间Agent流水线+生成午盘选股+风控+交易计划+最终决策 |
| 2026-07-06 | 📝 复盘 | 复盘记录/复盘_20260706.json | 日常复盘：偏差分析 + 策略建议 + 准确率趋势 |
| 2026-07-06 | 📝 fix | scripts/utils/backfill_ths_daily.py | ths_daily概念板块数据修复: THS AkShare回填55K行/118天+DB-first fallback链 |
| 2026-07-05 | 📝 feat | policy_analyst.py | Agent8持仓影响分析上线 — DeepSeek LLM驱动(24只全覆盖+分批+容错解析) |
| 2026-07-05 | 📝 feat | l2_rerank.py | L2 LLM排序配置持久化 — data/llm_config.json + CLI配置工具 |
| 2026-07-05 | 📝 feat | policy_analyst.py | Agent8上线完成 — SOCKS代理修复+证券时报新闻抓取fallback+自选股加载修复 |
| 2026-07-05 | 📝 feat | auto_sync.py | 达尔文16.0续 — moneyflow_stock全量历史回填(49K行, 20只持仓股从2014起全覆盖) |
| 2026-07-05 | 📝 add | knowledge/策略/数据源优先级.md | DS-49: 全局数据源优先级文档(各数据域主/备/兜底+Agent映射+限流策略) |
| 2026-07-05 | 📝 add | scripts/utils/_proxy.py | DS-48: 共享限流网关+代理抽取为scripts/utils/_proxy.py，跨模块协调东财请求间隔 |
| 2026-07-05 | 📝 fix | scripts/agent7-决策/leader.py | DF-26: detect_conflicts/市场判断优先读结构化JSON(目录 data/raw)，后降级Markdown正则 |
| 2026-07-05 | 📝 fix | scripts/agent5-选股/stock_picker.py | DF-23/24: score_technical优先读daily_indicator缓存，score_sentiment优先读moneyflow_stock缓存 |
| 2026-07-05 | 📝 fix | scripts/utils/tushare_client.py | HIGH修复: 延迟初始化(pro→get_pro()懒加载)，无Token时导入不崩溃，返回空DataFrame降级 |
| 2026-07-05 | 📝 fix | scripts/agent5-选股/stock_picker.py | HIGH修复: net_hsgt→north_money字段名修复(北向资金盘中/午盘模式始终为0) |
| 2026-07-05 | 📝 fix | scripts/utils/db_manager.py | MEDIUM修复: daily_indicator CREATE TABLE同步更新28列(rsi_oversold/rsi_overbought/obv系/boll_break系) |
| 2026-07-05 | 📝 fix | scripts/agent_ask/ask.py | MEDIUM修复: 移除rsi_6残留引用(DB只有rsi_14，rsi_6永远fallback到rsi14，导致短期=长期错误结论) |
| 2026-07-05 | 📝 fix | scripts/utils/auto_sync.py | CRITICAL修复: INDICATOR_COLS/BOOL_COLS/STR_COLS缺少OBV等10列，每日同步不填充OBV |
| 2026-07-05 | 📝 fix | scripts/utils/backfill_indicators.py | CRITICAL修复: INDICATOR_COLS缺少10列(包含obv/obv_ma20/obv_trend/obv_divergence等)，运行backfill也不填充OBV数据 |
| 2026-07-05 | 📝 fix | scripts/utils/risk_overlay.py | CRITICAL修复: MacdWeakCheck字段名错误(MACD_diff→macd_diff, MACD_dea→macd_signal)导致MACD风控永远返回0分 |
| 2026-07-05 | 📝 修复 | skills/agent8-政策分析师/SKILL.md | D4 CHECKPOINT删除重复CP5条目(5章节vs6章节矛盾) |
| 2026-07-05 | 📝 修复 | skills/agent7-投资领导/SKILL.md | 质量审核标准新增Agent8+Agent9专属审核项+描述更新(Agent1-9+问股) |
| 2026-07-05 | 📝 修复 | skills/agent4-复盘师/SKILL.md | 关联Agent表新增Agent8+Agent9上游输入 |
| 2026-07-05 | 📝 修复 | skills/agent2-分析师/SKILL.md | 关联Agent表新增Agent8(政策分析师)+Agent9(游资追踪师)上游输入 |
| 2026-07-05 | 📝 修复 | utils/technical_analysis.py | OBV计算NaN安全处理：np.nan_to_num防NoneVolume+OHLC矛盾自动修复 |
| 2026-07-05 | 📝 修复 | utils/auto_sync.py | 新增Phase 11: 全市场技术指标计算(daily_indicator)。新增sync_daily_indicators函数，更新docstring和--type choices |
| 2026-07-05 | 📝 添加 | utils/backfill_indicators.py | 新建全市场技术指标回填脚本，批量计算MACD/KDJ/RSI/BOLL/MA，支持全量回填和每日增量 |
| 2026-07-05 | 📝 修复 | utils/technical_analysis.py | OHLC矛盾修复：close>high→high=close, low>close→low=close，在指标计算层自动修复 |
| 2026-07-05 | 📝 📝 架构 | knowledge/INDEX.md | 达尔文12.0 — INDEX.md修复(孤立行移除+复盘07-04记录补全+日期更新) |
| 2026-07-05 | 📝 feat | scripts/utils/auto_sync.py | 新增Phase10: post_sync_data_check覆盖度检查+多源回补+数据清洗 |
| 2026-07-05 | 📝 feat | scripts/utils/backfill_etf_index.py | 新建一次性回填脚本(ETF+指数全量历史数据) |
| 2026-07-05 | 📝 feat | scripts/utils/auto_sync.py | 新增ETF/指数同步函数(sync_fund_basic/sync_index_basic/sync_etf_daily)+扩展sync_index_daily+流水线10阶段 |
| 2026-07-05 | 📝 feat | scripts/utils/db_manager.py | 新增 fund_basic + index_basic 元数据表和 upsert/query 方法 |
| 2026-07-05 | 📝 知识库 | skills/agent3-风控官/SKILL.md | Agent3扩展Lockup Watcher+基本面风控职责 |
| 2026-07-05 | 📝 知识库 | knowledge/策略/a-stock-data借鉴分析.md | 新增a-stock-data技术方案借鉴分析 |
| 2026-07-05 | 📝 知识库 | knowledge/策略/游资追踪策略.md | 新增游资追踪策略文件 |
| 2026-07-05 | 📝 知识库 | knowledge/策略/政策分析策略.md | 新增政策分析策略文件 |
| 2026-07-05 | 📝 Agent | scripts/agent9-游资追踪/hot_money_tracker.py | 新增Agent9游资追踪师（吸收TradingAgents-astock Hot Money Tracker） |
| 2026-07-05 | 📝 Agent | scripts/agent8-政策分析/policy_analyst.py | 新增Agent8政策分析师（吸收TradingAgents-astock Policy Analyst） |
| 2026-07-05 | 📝 数据层 | scripts/utils/data_provider.py | 升级多级数据源优先级系统（Tushare→mootdx→AkShare） |
| 2026-07-05 | 📝 数据层 | scripts/utils/mootdx_provider.py | 新增mootdx TCP免费数据源（吸收a-stock-data） |
| 2026-07-05 | 📝 数据层 | scripts/utils/eastmoney_get.py | 新增em_get()东方财富限流网关（吸收a-stock-data反爬设计） |
| 2026-07-04 | 📝 复盘 | 复盘记录/复盘_20260703.json | 日常复盘：偏差分析 + 策略建议 + 准确率趋势 |
| 2026-07-04 | 📝 🧬 研究 | knowledge/LLM-Wiki-工作法.md | 深度研究Karpathy原Gist:Memex溯源+维护瓶颈分析+回答复利原则+日志格式/搜索工具/Obsidian生态 |
| 2026-07-04 | 🔧 研究 | LLM-Wiki-工作法.md | 深度研究Karpathy原Gist，补充四大深度内容：①Memex历史渊源 ②"为什么有效"维护瓶颈分析 ③"好的回答可成为知识"复利原则 ④日志格式/搜索工具/Obsidian生态 ⑤修正章节编号+补充深度研究日期
| 2026-07-04 | ✏️ 修正 | knowledge/INDEX.md | 审计修复：重复引用消除+不存在目录修正+元文件章节新增+外部引用补齐 |
| 2026-07-04 | ✏️ 修正 | data/止损规则.json | 审计修复：三级止损命名统一（固定比例止损→固定比例止损_中性） |
| 2026-07-04 | ✏️ 修正 | data/仓位管理规则.json | 审计修复：偏移量公式添加说明（正偏移=更严格） |
| 2026-07-04 | ✏️ 修正 | knowledge/策略/选股策略.md | 审计修复：D4检查点升级到8因子体系+Risk Overlay联动 |
| 2026-07-04 | ✏️ 修正 | 复盘记录/复盘_20260703.json | 审计修复：comparison.correct=null→false，偏差分析补充 |
| 2026-07-04 | 🧬 因子 | scripts/utils/knowledge_lint.py | 审计修复：Windows GBK编码兼容（sys.stdout.reconfigure） |
| 2026-07-04 | 📝 配置修复 | data/策略规则.json | 达尔文10.0 — 补充流动性/稳定性/反转因子信号定义 |
| 2026-07-04 | 📝 代码修复 | scripts/agent4-复盘/review.py | 达尔文10.0 — 消除2处硬编码路径改用p()函数+修复冗余pass |
| 2026-07-04 | 📝 架构 | knowledge/策略/选股策略.md | 达尔文10.0 — 修复因子章节权重硬编码+输出模板补全8因子字段 |
| 2026-07-04 | 📝 架构 | CLAUDE.md | 达尔文10.0 — 15项一致性修复:添加Agent-问股行/目录结构/webui/GitHub Actions/本地命令/策略知识库扩展 |
| 2026-07-04 | 📝 架构 | 41个Python脚本 | 达尔文10.0 — 消除~30处silent except:pass为[WARN]输出+统一PROJECT_ROOT定义+修复死代码 |
| 2026-07-04 | 📝 架构 | skills/agent-问股/SKILL.md | 达尔文10.0 — D3 5→8条/D4 5→7个/D9 5→9条+补全4个缺失章节(关联Agent/打回重做/工作原则/推送通知) |
| 2026-07-04 | 📝 架构 | knowledge/INDEX.md | 达尔文10.0 — INDEX.md新增工作规范+策略知识库扩展+CLAUDE.md同步 |
| 2026-07-04 | 📝 复盘 | 复盘记录/复盘_20260703.json | 日常复盘：偏差分析 + 策略建议 + 准确率趋势 |
| 2026-07-04 | 📝 架构 | data/选股规则.json | 升级5因子→8因子体系+新增scoring_profile评分曲线配置(借鉴AlphaSift) |
| 2026-07-04 | 📝 架构 | scripts/agent5-选股/stock_picker.py | 新增流动性/稳定性/反转3因子评分+非线性评分曲线函数+8因子权重计算 |
| 2026-07-04 | 📝 架构 | scripts/agent5-选股/stock_picker.py | L1→L2→L3管线重构: L1评分→L2重排序→L3后置分析器 |
| 2026-07-04 | 📝 架构 | scripts/utils/scorecard.py | 新增L3 Scorecard后置分析器: 突破/量价/均线/板块/基本面5项规则 |
| 2026-07-04 | 📝 架构 | scripts/utils/risk_overlay.py | 新增风险叠加层(借鉴AlphaSift): 涨跌/量比/PE/MACD/PB/连跌6项独立惩罚 |
| 2026-07-04 | 📝 架构 | scripts/utils/data_provider.py | 新增多数据源Provider层: Tushare+AkShare自动fallback |
| 2026-07-04 | 📝 架构 | .github/workflows/00-daily-analysis.yml | 新增GitHub Actions零成本部署(借鉴daily_stock_analysis) |
| 2026-07-04 | 📝 配置 | data/选股规则.json | 新增scoring_profile+8因子权重+热点板块关键词外部化 |
| 2026-07-04 | 📝 知识 | knowledge/策略/选股策略.md | 升级到8因子体系描述+新增评分曲线配置+风险叠加层说明 |
| 2026-07-04 | 📝 架构 | scripts/utils/l2_rerank.py | L2 LLM相对排序引擎: LLM/规则混合排序+自动降级(借鉴AlphaSift) |
| 2026-07-04 | 📝 架构 | scripts/agent_ask/ask.py | Agent策略问股系统: 9个策略模板(均线/缠论/波浪/MACD/量价/RSI/布林/KDJ/综合) |
| 2026-07-04 | 📝 架构 | skills/agent-问股/SKILL.md | 问股Agent技能: 自然语言问股触发词+D3/D4/D9 |
| 2026-07-04 | 📝 配置 | .env.example | 新增LLM配置项(LLM_PROVIDER/MODEL/API_KEY/API_BASE) |
| 2026-07-04 | 📝 配置修复 | skills/agent2-分析师/SKILL.md | tavily-search→WebSearch+cron同步CLAUDE.md |
| 2026-07-04 | 📝 配置修复 | skills/agent1-情报员/SKILL.md | tushare-data引用移除+cron同步CLAUDE.md |
| 2026-07-04 | 📝 配置修复 | skills/agent7-投资领导/SKILL.md | 步骤编号重复修复(第四/五/六/七步重新编号) |
| 2026-07-04 | 📝 配置修复 | .mcp.json | 替换为./，兼容Claude Code MCP runner |
| 2026-07-04 | 🔧 新增 | webui/ | WebUI管理界面: FastAPI后端+5路由+6页面+Bootstrap5+HTMX+Chart.js(借鉴daily_stock_analysis) |
| 2026-07-04 | 📝 代码修复 | scripts/agent6-操盘/trader.py | 2处静默pass消除+迭代修改列表修复+正则增强+无用import移除 |
| 2026-07-04 | 📝 代码修复 | scripts/agent2-技术分析/analyze.py | 3处静默pass改为WARN+get_ths_index异常输出+market_summary防None |
| 2026-07-04 | 📝 代码修复 | scripts/agent3-风控/risk_check.py | load_json JSONDecodeError保护+2处静默pass消除+死代码删除 |
| 2026-07-04 | 📝 代码修复 | scripts/agent5-选股/stock_picker.py | 行业集中度改用实际industry字段+新增get_stock_industry函数 |
| 2026-07-04 | 📝 代码修复 | scripts/agent4-复盘/review.py | 准确率trend key兼容+env_score correct修复+无提示except警告 |
| 2026-07-04 | 📝 代码修复 | scripts/agent7-决策/leader.py | load_json/load_report异常保护+冗余import移除 |
| 2026-07-04 | 📝 代码修复 | scripts/agent7-决策/debate.py | 达尔文8.0: debate.py新增D3/D4/D9完整覆盖+load_json try/except |
| 2026-07-04 | 📝 架构升级 | SQLite决策审计日志 | db_manager.py新增decision_log表和log_decision()方法，leader.py每次决策自动记录 |
| 2026-07-04 | 📝 架构升级 | BullBear对抗辩论 | 新增scripts/agent7-决策/debate.py多空对抗辩论模块，leader.py自动调用 |
| 2026-07-04 | 📝 架构升级 | 决策记忆注入 | review.py写入memory/决策反思.md供Agent7次日自动加载，TradingAgents记忆机制本土化 |
| 2026-07-04 | 📝 架构升级 | 三级风控委员会 | risk_check.py支持--risk-profile三档位(激进/中性/保守)，仓位管理规则.json止损规则.json增加对应配置 |
| 2026-07-04 | 📝 新知 | TradingAgents借鉴分析.md | TradingAgents架构深度分析，7项可借鉴改进+P0/P1/P2分级方案 |
| 2026-07-04 | 📝 回测 | 含退市股全策略回测报告.md | 全策略含退市股回测报告 |
| 2026-07-03 | 📝 研究 | 策略/MA96-RSI策略分析.md | MA96+RSI(14)策略深度分析与回测：96源于5分钟K线，日线建议改用MA120 |
| 2026-07-03 | 🔧 新增 | LLM-Wiki-工作法.md | 安装Karpathy LLM Wiki工作法：编译而非检索的知识管理范式 |
| 2026-07-03 | ✏️ 修正 | 策略/交易执行规则.md | 添加最后更新日期标注 |
| 2026-07-03 | ✏️ 修正 | 策略/择时策略.md | 添加最后更新日期标注 |
| 2026-07-03 | ✏️ 修正 | 策略/选股策略.md | 添加最后更新日期标注+INDEX.md更新日期列 |
| 2026-07-03 | 🔧 新增 | knowledge_lint.py | 知识库一致性检查脚本（孤页/断裂引用/过时/矛盾检测） |
| 2026-07-03 | 🔧 新增 | INDEX.md | 创建知识库索引（含文件引用关系图+双向引用表） |
| 2026-07-03 | 🔧 新增 | CHANGES.md | 创建知识库变更日志 |
| 2026-07-03 | 📝 复盘 | 复盘记录/复盘_20260703.json | 日常复盘：偏差分析 + 投资领导审核 + 环境评分分离问题记录 |

---

## 变更类型标签

| 标签 | 含义 |
|:----|:------|
| 📝 复盘 | 日常复盘记录更新 |
| 📊 选股 | 选股策略调整（权重、因子） |
| 📈 择时 | 择时策略更新（入场/出场规则） |
| 🎯 交易 | 交易执行规则更新 |
| 🛡️ 风控 | 风控规则调整 |
| 🧬 因子 | 因子体系变更（新增/删除/权重调整/曲线配置） |
| 🔧 新增 | 新建文件/目录 |
| ✏️ 修正 | 内容错误修正 |
| 🔄 重构 | 组织结构调整 |
| ❌ 废弃 | 标记为过时 |
