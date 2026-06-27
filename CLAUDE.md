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
- **职责**：基于情报热点+技术面+基本面因子，筛选候选股票池
- **输出**：`reports/日报/选股/选股建议_YYYY-MM-DD.md`
- **配置**：`data/选股规则.json` + `knowledge/策略/选股策略.md`
- **D3异常**：6条fallback（行情失败、无候选票、财务数据缺失等）
- **D4检查**：候选票多因子交叉验证、持仓冲突检查、否决记录
- **D9反例**：禁止单一因子决策、追涨、忽视持仓冲突

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
  │   ├── claw/        - 定时调度 MCP 服务器 (cron)
  │   ├── telegram/    - 通知推送 MCP 服务器 (Telegram)
  │   ├── wechat/      - 企业微信通知推送
  │   ├── qq/          - QQ通知推送 (PushPlus + SMTP)
  │   └── qqbot/       - QQ机器人官方API (WebSocket)
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
| 07:00 工作日 | Agent1 情报采集 | `0 7 * * 1-5` | claw MCP + Python脚本 → Telegram+微信+QQ推送 |
| 08:30 工作日 | Agent2 技术分析 | `30 8 * * 1-5` | claw MCP + Python脚本 → Telegram+微信+QQ推送 |
| 21:00 工作日 | Agent4 复盘分析 | `0 21 * * 1-5` | claw MCP + Python脚本 → Telegram+微信+QQ推送 |
| 按需 | Agent3 风控检查 | - | 手动 `/风控官` → Telegram+微信+QQ推送 |
| 按需 | Agent5 选股机器人 | - | 手动 `/选股` → Telegram+微信+QQ推送 |
| 按需 | Agent6 操盘手 | - | 手动 `/操盘` → Telegram+微信+QQ推送 |
| 按需 | Agent7 投资领导 | - | 手动 `/决策` → Telegram+微信+QQ推送 |

> MCP server: `.claude/mcp-servers/claw/server.js` (stdio JSON-RPC)
> 工具: `mcp__claw__cron` (创建) / `cron_list` (查询) / `cron_delete` (删除)

## 📱 通知推送（Telegram + 微信 + QQ）

每个 Agent 在生成报告后，自动推送摘要到手机。支持三个通道：Telegram（国际通用）、微信（国内首选）和 QQ（备用方案）。

### Telegram 通道

**MCP 服务器**：`.claude/mcp-servers/telegram/server.js`（自动发现）

**提供工具**：
- `telegram_send_message` — 发送文本摘要（Markdown 格式）
- `telegram_send_file` — 发送完整报告文件（.md 文档）

**配置**（在 `.claude/settings.local.json` 的 `env` 中填写）：
```json
"TELEGRAM_BOT_TOKEN": "你的Bot Token（从 @BotFather 获取）",
"TELEGRAM_CHAT_ID": "你的Chat ID（从 @userinfobot 获取）"
```

### 微信通道

**MCP 服务器**：`.claude/mcp-servers/wechat/server.js`（自动发现）

**提供工具**：
- `wechat_send_text` — 发送纯文本（可指定 `agent_id` 选择机器人）
- `wechat_send_markdown` — 发送 Markdown（可指定 `agent_id`）
- `wechat_agent_notify` — 按 Agent 编号发送（自动带角色名称前缀）

**支持4个独立机器人！** 每个 Agent 使用自己专属的机器人推送：

| 机器人 | 字段 | Agent | 机器人名称建议 |
|-------|------|-------|--------------|
| 🤖 1号 | `WECHAT_WEBHOOK_1` | 🕵️ 情报员 | "情报员" |
| 🤖 2号 | `WECHAT_WEBHOOK_2` | 📊 分析师 | "分析师" |
| 🤖 3号 | `WECHAT_WEBHOOK_3` | 🛡️ 风控官 | "风控官" |
| 🤖 4号 | `WECHAT_WEBHOOK_4` | 🔄 复盘师 | "复盘师" |
| 通用 | `WECHAT_WEBHOOK_URL` | 兼容旧配置 | — |

**配置方法**：

1. 在企业微信中 **新建一个群**（或使用现有群）
2. 群设置 → 群机器人 → **添加机器人**（可添加最多7个）
3. 每个机器人设置不同的名称和头像（情报员/分析师/选股机器人/风控官/操盘手/复盘师/投资领导）
4. 分别复制 Webhook URL，填入 `.claude/settings.local.json`：

```json
// 每个 Agent 各一个机器人（推荐）
"WECHAT_WEBHOOK_1": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx",  // 情报员
"WECHAT_WEBHOOK_2": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx",  // 分析师
"WECHAT_WEBHOOK_3": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx",  // 风控官
"WECHAT_WEBHOOK_4": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx",  // 复盘师
"WECHAT_WEBHOOK_5": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx",  // 选股机器人
"WECHAT_WEBHOOK_6": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx",  // 操盘手
"WECHAT_WEBHOOK_7": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx",  // 投资领导

// 或只配一个通用机器人（所有Agent共用）
"WECHAT_WEBHOOK_URL": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx"
```

### QQ 通道（PushPlus）

**MCP 服务器**：`.claude/mcp-servers/qq/server.js`（自动发现）

**提供工具**：
- `qq_send_text` — 发送纯文本通知到 QQ（PushPlus 推送到绑定 QQ）
- `qq_send_report` — 发送完整报告（Markdown 格式渲染）
- `qq_agent_notify` — 按 Agent 编号发送（自动带角色名称前缀）

**配置方法（PushPlus，推荐）：**
1. 打开 [PushPlus 官网](https://pushplus.hxtrip.com) 微信扫码登录
2. 在 **个人中心** 获取你的推送 Token
3. 在 **推送配置** → **QQ好友** 或 **QQ群** 中绑定接收通知的目标
4. 在 `.claude/settings.local.json` 的 `env` 中填写：

```json
"PUSHPLUS_TOKEN": "你的PushPlus Token（从 pushplus.hxtrip.com 获取）"
```

**备用方案（QQ邮箱 SMTP）：**
如果不想用 PushPlus，也可以直接用 QQ邮箱 SMTP 触发手机推送：

```json
"QQ_MAIL_USER": "你的QQ号@qq.com",
"QQ_MAIL_PASS": "你的SMTP授权码（QQ邮箱 → 设置 → 账户 → 生成授权码）"
```

> QQ邮箱方式收到的是普通邮件通知，PushPlus 方式可以在 QQ 好友/群中直接显示消息。

### QQ 机器人通道（官方 API）

**MCP 服务器**：`.claude/mcp-servers/qqbot/server.js`（自动发现）

**提供工具**：
- `qqbot_send_text` — 发送文本消息到 QQ（单聊/群聊）
- `qqbot_send_markdown` — 发送 Markdown 格式消息
- `qqbot_agent_notify` — 按 Agent 编号发送（自动带角色名称前缀）
- `qqbot_listen` — 事件监听模式（获取用户 OpenID）

**特点：**
- 使用 [QQ 开放平台](https://q.qq.com) 官方 API v2
- 基于 WebSocket 持久连接，机器人需要保持在线
- ⚠️ **主动消息每月仅 4 条/用户**，适合交互式查询不适合每日自动推送
- 推荐用于 `/风控官` 等按需查询场景

**配置方法：**
1. 访问 [QQ 开放平台](https://q.qq.com) 创建机器人，获取 AppID 和 AppSecret
2. 在 `.claude/settings.local.json` 的 `env` 中填写：

```json
"QQBOT_APP_ID": "你的机器人AppID",
"QQBOT_APP_SECRET": "你的机器人AppSecret",
"QQBOT_TARGET_OPENID": "目标用户的OpenID（可选，配好前两个后调用 qqbot_listen 获取）"
```

**获取用户 OpenID：**
```bash
# MCP 工具方式（在 Claude Code 中执行）：
# 调用 qqbot_listen(duration=120)
# 然后用手机 QQ 加机器人好友并发送一条消息
# 系统会自动检测到用户的 OpenID
```

### 🔄 常驻监听模式（自动响应手机命令）

Telegram 和 QQ Bot 支持**后台常驻监听**，启动后自动检测手机发来的命令并执行对应 Agent，无需手动操作。

**双通道常驻服务：**

| 服务 | 启动方式 | 监听端口 | 文件 |
|------|---------|---------|------|
| 📱 Telegram 监听 | `start-telegram.bat` 或 `node .claude/mcp-servers/telegram/keepalive.js` | 19786 | `.claude/mcp-servers/telegram/keepalive.js` |
| 💬 QQ Bot 监听 | `start-qqbot.bat` 或 `node .claude/mcp-servers/qqbot/keepalive.js` | 19785 | `.claude/mcp-servers/qqbot/keepalive.js` |

**使用方法：**
1. 双击 `start-telegram.bat` 或 `start-qqbot.bat` 启动（保持窗口打开）
2. 在手机 Telegram/QQ 中给机器人发命令：`/情报员`、`/分析师`、`/风控官`、`/复盘师`
3. 机器人自动运行对应的 Python 脚本并回复结果
4. 按 `Ctrl+C` 停止监听

> 💡 **建议**：每日自动推送用 PushPlus（无限制），QQ 机器人用于您主动询问时的交互式回复（被动回复无限制）。常驻监听模式启动后，手机发命令即可触发 Agent，无需打开 Claude Code。

### 推送时机

| Agent | 触发时间 | 推送内容 |
|-------|---------|---------|
| 🕵️ 情报员 | 07:00 报告生成后 | 大盘概况 + 关键资讯 + 热点板块 |
| 📊 分析师 | 08:30 报告生成后 | 大盘评分 + 强势板块 + 操作建议 |
| 🛡️ 风控官 | 按需/盘中 | 风险等级 + 止损/仓位预警 |
| 🔍 选股机器人 | 按需 | 候选股票池 + 多因子评分 |
| 🎯 操盘手 | 按需 | 买入/卖出/持有清单 |
| 🏆 投资领导 | 按需 | 最终投资决策 + 团队调度 |
| 🔄 复盘师 | 21:00 报告生成后 | 综合准确率 + 偏差总结 + 知识库更新 |

> 所有四个通道（Telegram / 微信 / QQ-PushPlus / QQ机器人）都是可选的，配置哪个就用哪个，未配置的通道自动跳过。

## 交互式命令（手机端触发）

你可以在 **Telegram** 或 **QQ Bot** 中给机器人发送命令，Claude Code 检测到后自动执行对应的 Agent。

### 支持的手机命令

| 手机命令 | 触发Agent | 说明 | 支持通道 |
|---------|----------|------|---------|
| `/情报员` | agent1-情报员 | 情报采集+报告生成 | Telegram, QQ Bot |
| `/分析师` | agent2-分析师 | 技术分析+板块排名 | Telegram, QQ Bot |
| `/风控官` | agent3-风控官 | 风控检查+止损监控 | Telegram, QQ Bot |
| `/复盘师` | agent4-复盘师 | 复盘+偏差分析+知识库更新 | Telegram, QQ Bot |
| `/选股` 或 `/选股机器人` | agent5-选股机器人 | 多因子选股+评分排名 | Telegram, QQ Bot |
| `/操盘` 或 `/操盘手` | agent6-操盘手 | 交易计划+仓位分配 | Telegram, QQ Bot |
| `/决策` 或 `/投资领导` | agent7-投资领导 | 综合决策+团队调度 | Telegram, QQ Bot |

### 交互流程

**方式一：常驻监听（推荐）**
```
启动 bat → 手机发命令 → 机器人自动执行 → 回复到手机
```
双击 `start-telegram.bat` 或 `start-qqbot.bat` 保持后台运行，手机直接发命令即可。

**方式二：手动监听**
```
你发消息 → 你运行 listen 工具 → Claude 解析命令 → 执行 Agent → 回复到手机
```

### 使用方法

**Telegram：**
1. 双击 `start-telegram.bat` 启动常驻监听（或在 Claude Code 中调用 `telegram_listen(duration=60)`）
2. 在 Telegram 中给 `@Qby0001bot` 发送命令（如 `/情报员`）
3. 机器人自动运行脚本并发回结果

**QQ Bot：**
1. 双击 `start-qqbot.bat` 启动常驻监听（或在 Claude Code 中调用 `qqbot_listen(duration=60)`）
2. 在 QQ 中给机器人发送命令（如 `/情报员`）
3. 机器人自动运行脚本并发回结果

> 微信企业微信机器人和 QQ PushPlus 仅支持单向推送，不支持接收消息互动。

### 本地命令

| 命令 | 触发Skill | 说明 |
|------|----------|------|
| `/情报员` | agent1-情报员 | 情报采集+报告生成 |
| `/分析师` | agent2-分析师 | 技术分析+板块排名 |
| `/风控官` | agent3-风控官 | 风控检查+止损监控 |
| `/复盘师` | agent4-复盘师 | 复盘+偏差分析+知识库更新 |

---

## 🔄 Telegram / QQ Bot ↔ Claude Code 双向交互（cc-bridge 架构）

项目通过 `cc-bridge` 模块实现手机端和 Claude Code AI 之间的实时双向对话。

### 架构（借鉴 cc-connect）

```
手机发消息 ──→ keepalive.js ──→ cc-bridge ──→ Claude Code CLI (子进程)
  (Telegram/QQ)       │            │          │  --print --resume <sid>
                      │            │          │  stdin: "用户问题"
                      │            │          │  stdout: stream-json
                      │            │          ▼
                      │            │     AI 处理 + 上下文延续
                      │            │          │
                      │            ▼          │
                      └────←─── 回复到手机 ←──┘
                         实时推送
```

### 核心模块：`cc-bridge/`

| 文件 | 角色 |
|------|------|
| `cc-bridge/index.js` | `ClaudeCodeSession` 类：管理 Claude Code 子进程生命周期 |
| `.cc-bridge/session_id` | 持久化会话 ID，重启后上下文不丢失 |

### 工作原理

1. **子进程模式**：每次 `send()` 启动一个 `claude --print` 进程
2. **上下文延续**：通过 `--session-id` + `--resume` 保持多轮对话上下文
3. **结构化输出**：`--output-format stream-json` 输出 NDJSON，无需解析终端
4. **自动重启**：进程崩溃后自动恢复

### 两种响应路径

| 路径 | 实现 | 响应速度 | 示例 |
|------|------|---------|------|
| **预设命令** | keepalive 直接 spawn Python | ~2秒 | `/情报员` `/分析师` |
| **AI 对话** | keepalive → cc-bridge → Claude Code | ~5-15秒 | "帮我看看XX股票" |

### 数据流示例

```
手机发 "帮我看看XX股票的基本面"
  ↓ keepalive 检测到（非命令消息）
  ├── 发送 "🧠 正在思考，请稍候..."
  └── 调用 ccSession.send("帮我看看XX股票的基本面")
        ↓
      cc-bridge:
        ├── spawn claude --print --resume <sid> "帮我看看XX股票的基本面"
        ├── 解析 stdout 中的 stream-json 事件
        ├── 提取 text 回复内容
        └── 返回 { text: "...", cost: 0.04 }
        ↓
  └── 发送回复到手机
手机收到 AI 回复
```

### 技术细节

- **CLI 参数**：`--print --output-format stream-json --verbose --resume <sid> --bare`
- **会话续传**：首次用 `--append-system-prompt`，后续用 `--resume`
- **超时控制**：180 秒，超时自动重试
- **成本追踪**：每次回复返回 token 消耗和费用（美元）
- **platforms**：Telegram（keepalive.js）和 QQ Bot（qqbot/keepalive.js）共享同一 cc-bridge 实例

## Security

- **Token 管理**：Tushare Token 存储在 `.claude/settings.local.json`（已在 `.gitignore` 中排除）
- **环境变量**：所有 Token 通过 settings 的 `env` 字段注入，不在脚本中硬编码
- **Git 清理**：已执行 `git filter-branch` 清除历史中的所有 token 痕迹
- **批处理文件**：`run-agent*.bat` 不包含任何凭证，依赖自动加载的环境变量
- **Telegram 通知**：Bot Token 和 Chat ID 同样存储在 `.claude/settings.local.json`（已在 `.gitignore` 中排除）
- **QQ 通知（PushPlus）**：PushPlus Token 和 QQ邮箱授权码同样存储在 `.claude/settings.local.json`（已在 `.gitignore` 中排除）
- **QQ 机器人**：QQ Bot AppSecret 存储在 `.claude/settings.local.json`（已在 `.gitignore` 中排除）

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

优化内容：22条D3 fallback + 4个D4 CHECKPOINT + 21条D9反例 (原有4 Agent)
新增：Agent5-7全套D3/D4/D9 + 全团队D4升级 + 统一标准化格式 + 全团队test-prompts
