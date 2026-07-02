---
name: 投研-QQ通知推送
description: QQ通知推送配置——PushPlus通道（QQ MCP）
metadata:
  type: reference
  project: 股票投研自动化
---

# QQ 通知推送

## 通道配置

QQ 通知已作为可选通知通道加入项目（继 PushPlus 微信主通道之后）。

### PushPlus（推荐，已实施）
- **MCP 服务器**：`.claude/mcp-servers/qq/server.js`
- **配置**：`.claude/settings.local.json` 中设置 `env.PUSHPLUS_TOKEN`
- **注册**：https://pushplus.hxtrip.com → 微信扫码 → 个人中心获取 Token
- **绑定**：在 PushPlus 推送配置中绑定 QQ好友 或 QQ群
- **提供工具**：`qq_send_text`、`qq_send_report`、`qq_agent_notify`

> 注意：QQ邮箱SMTP方案未实施，无对应配置和脚本。QQ推送统一走 PushPlus API。

## Agent 通知更新
所有 7 个 Agent 的 SKILL.md 已添加 QQ 通知步骤（可选），在 PushPlus 微信推送下方：
- Agent1-7：各 SKILL.md 推送章节末尾

## 相关文件
- [[投研-项目目标]]
- [[投研-持仓信息]]
