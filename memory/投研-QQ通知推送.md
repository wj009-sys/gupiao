---
name: 投研-QQ通知推送
description: QQ通知推送配置——PushPlus和QQ邮箱SMTP双方案
metadata:
  type: reference
  project: 股票投研自动化
---

# QQ 通知推送

## 通道配置

QQ 通知已作为第三通知通道加入项目（继 Telegram、微信之后）。

### 方法一：PushPlus（推荐）
- **MCP 服务器**：`.claude/mcp-servers/qq/server.js`
- **配置**：`.claude/settings.local.json` 中设置 `env.PUSHPLUS_TOKEN`
- **注册**：https://pushplus.hxtrip.com → 微信扫码 → 个人中心获取 Token
- **绑定**：在 PushPlus 推送配置中绑定 QQ好友 或 QQ群
- **提供工具**：`qq_send_text`、`qq_send_report`、`qq_agent_notify`

### 方法二：QQ邮箱 SMTP（备用）
- **配置**：`QQ_MAIL_USER`（QQ邮箱地址）、`QQ_MAIL_PASS`（SMTP授权码）、`QQ_MAIL_TO`（目标邮箱，不填则发给自己）
- **授权码获取**：QQ邮箱 → 设置 → 账户 → 开启 SMTP → 生成授权码

## Agent 通知更新
所有 4 个 Agent 的 SKILL.md 已添加 QQ 通知步骤，在微信推送下方：
- Agent1 情报员：SKILL.md 第 229+ 行附近
- Agent2 分析师：SKILL.md 第 222+ 行附近
- Agent3 风控官：SKILL.md 第 219+ 行附近
- Agent4 复盘师：SKILL.md 第 283+ 行附近

## 相关文件
- [[投研-项目目标]]
- [[投研-持仓信息]]
