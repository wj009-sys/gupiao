# 📋 知识库变更日志

> 每次知识库更新自动追加，由复盘师(Agent4)维护。
> 格式：`YYYY-MM-DD | 类型 | 文件 | 变更摘要`

## 2026年

### 7月

| 日期 | 类型 | 文件 | 变更摘要 |
|:----|:----|:-----|:--------|
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
