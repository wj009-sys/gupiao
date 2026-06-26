#!/usr/bin/env node
/**
 * WeChat MCP Server — 推送通知到微信
 *
 * 支持 4 个独立机器人的企业微信群，每个 Agent 使用自己的机器人推送。
 * 也兼容单机器人模式。
 *
 * 提供工具:
 *   - wechat_send_text:      发送纯文本（可指定 agent_id 选择机器人）
 *   - wechat_send_markdown:  发送 Markdown（可指定 agent_id）
 *   - wechat_agent_notify:   按 Agent 编号发送（自动带 Agent 名称前缀）
 *
 * 环境变量（企业微信群机器人 Webhook URL，至少配一个）:
 *   WECHAT_WEBHOOK_1  — Agent1 情报员
 *   WECHAT_WEBHOOK_2  — Agent2 分析师
 *   WECHAT_WEBHOOK_3  — Agent3 风控官
 *   WECHAT_WEBHOOK_4  — Agent4 复盘师
 *   WECHAT_WEBHOOK_URL — 默认/兼容旧配置
 *
 * 依赖: 无（仅使用 Node.js 内置 https 模块）
 * MCP 协议: stdio JSON-RPC (v2024-11-05)
 */

const https = require("https");
const { URL } = require("url");

// ============ 配置 ============

const AGENT_WEBHOOKS = {
  1: process.env.WECHAT_WEBHOOK_1 || "",
  2: process.env.WECHAT_WEBHOOK_2 || "",
  3: process.env.WECHAT_WEBHOOK_3 || "",
  4: process.env.WECHAT_WEBHOOK_4 || "",
};
const DEFAULT_WEBHOOK = process.env.WECHAT_WEBHOOK_URL || "";

const AGENT_NAMES = {
  1: "情报员",
  2: "分析师",
  3: "风控官",
  4: "复盘师",
};

/** 获取指定 agent 的 webhook URL */
function getWebhookUrl(agentId) {
  if (agentId && AGENT_WEBHOOKS[agentId]) return AGENT_WEBHOOKS[agentId];
  if (DEFAULT_WEBHOOK) return DEFAULT_WEBHOOK;
  // 回退到任意已配置的
  for (let i = 1; i <= 4; i++) {
    if (AGENT_WEBHOOKS[i]) return AGENT_WEBHOOKS[i];
  }
  return null;
}

/** 检查是否有任何 webhook 已配置 */
function hasAnyWebhook() {
  if (DEFAULT_WEBHOOK) return true;
  for (let i = 1; i <= 4; i++) {
    if (AGENT_WEBHOOKS[i]) return true;
  }
  return false;
}

/** 检测已配置的机器人数量 */
function getConfiguredCount() {
  let count = 0;
  if (DEFAULT_WEBHOOK) count++;
  for (let i = 1; i <= 4; i++) {
    if (AGENT_WEBHOOKS[i]) count++;
  }
  return count;
}

// ============ 工具定义 ============

const TOOLS = [
  {
    name: "wechat_send_text",
    description: "发送纯文本消息到企业微信群。可指定 agent_id 选择由哪个机器人发送（1=情报员, 2=分析师, 3=风控官, 4=复盘师）。",
    inputSchema: {
      type: "object",
      properties: {
        content: {
          type: "string",
          description: "文本消息内容",
        },
        agent_id: {
          type: "number",
          description: "机器人编号：1=情报员, 2=分析师, 3=风控官, 4=复盘师（可选，默认使用通用机器人）",
          enum: [1, 2, 3, 4],
        },
      },
      required: ["content"],
    },
  },
  {
    name: "wechat_send_markdown",
    description: "发送 Markdown 格式消息到企业微信群。可指定 agent_id 选择机器人。",
    inputSchema: {
      type: "object",
      properties: {
        title: {
          type: "string",
          description: "消息标题（会作为加粗首行）",
        },
        content: {
          type: "string",
          description: "Markdown 格式正文",
        },
        agent_id: {
          type: "number",
          description: "机器人编号：1=情报员, 2=分析师, 3=风控官, 4=复盘师（可选）",
          enum: [1, 2, 3, 4],
        },
      },
      required: ["title", "content"],
    },
  },
  {
    name: "wechat_agent_notify",
    description: "按 Agent 发送通知，自动带 Agent 角色名称前缀。适合 Agent 工作流结束时调用。",
    inputSchema: {
      type: "object",
      properties: {
        agent_id: {
          type: "number",
          description: "机器人编号：1=情报员, 2=分析师, 3=风控官, 4=复盘师",
          enum: [1, 2, 3, 4],
        },
        content: {
          type: "string",
          description: "通知正文内容（不含 Agent 名称，系统自动加前缀）",
        },
      },
      required: ["agent_id", "content"],
    },
  },
];

// ============ 企业微信 API 调用 ============

function sendQywx(webhookUrl, msgType, payload) {
  return new Promise((resolve, reject) => {
    const url = new URL(webhookUrl);
    const body = JSON.stringify({
      msgtype: msgType,
      [msgType]: payload,
    });

    const req = https.request(
      {
        hostname: url.hostname,
        path: url.pathname + url.search,
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Content-Length": Buffer.byteLength(body),
        },
        timeout: 15000,
      },
      (res) => {
        let data = "";
        res.on("data", (c) => (data += c));
        res.on("end", () => {
          try {
            const parsed = JSON.parse(data);
            if (parsed.errcode === 0) resolve(parsed);
            else reject(new Error(`企业微信错误: ${parsed.errmsg} (code=${parsed.errcode})`));
          } catch {
            reject(new Error(`解析响应失败: ${data.slice(0, 200)}`));
          }
        });
      }
    );
    req.on("error", reject);
    req.on("timeout", () => { req.destroy(); reject(new Error("请求超时")); });
    req.write(body);
    req.end();
  });
}

// ============ MCP 协议处理 ============

async function handleMessage(msg) {
  const { id, method, params } = msg;

  switch (method) {
    case "initialize":
      return {
        id,
        result: {
          protocolVersion: "2024-11-05",
          capabilities: { tools: {} },
          serverInfo: { name: "wechat-notifier", version: "2.0.0" },
        },
      };

    case "notifications/initialized":
      return null;

    case "tools/list":
      return { id, result: { tools: TOOLS } };

    case "tools/call": {
      const toolName = params.name;
      const args = params.arguments || {};

      // 检查配置
      if (!hasAnyWebhook()) {
        return {
          id,
          error: {
            code: -32000,
            message:
              "未配置企业微信群机器人 Webhook。请至少配置一个：\n" +
              "在 .claude/settings.local.json 中设置 env.WECHAT_WEBHOOK_1~4（每个Agent独立机器人）\n" +
              "或 env.WECHAT_WEBHOOK_URL（通用机器人）\n\n" +
              "详细教程见 CLAUDE.md",
          },
        };
      }

      switch (toolName) {
        case "wechat_send_text": {
          const { content, agent_id } = args;
          if (!content) {
            return { id, error: { code: -32602, message: "缺少必填参数: content" } };
          }
          const webhookUrl = getWebhookUrl(agent_id);
          if (!webhookUrl) {
            return { id, error: { code: -32000, message: `Agent ${agent_id} 未配置 webhook URL` } };
          }
          try {
            const result = await sendQywx(webhookUrl, "text", { content });
            return {
              id,
              result: {
                content: [
                  {
                    type: "text",
                    text: JSON.stringify({
                      success: true,
                      agent: agent_id ? `${agent_id}:${AGENT_NAMES[agent_id] || ""}` : "default",
                      text_length: content.length,
                    }),
                  },
                ],
              },
            };
          } catch (err) {
            return { id, error: { code: -32000, message: `发送失败: ${err.message}` } };
          }
        }

        case "wechat_send_markdown": {
          const { title, content, agent_id } = args;
          if (!title || !content) {
            return { id, error: { code: -32602, message: "缺少必填参数: title, content" } };
          }
          const webhookUrl = getWebhookUrl(agent_id);
          if (!webhookUrl) {
            return { id, error: { code: -32000, message: `Agent ${agent_id} 未配置 webhook URL` } };
          }
          try {
            const fullContent = `## ${title}\n${content}`;
            const result = await sendQywx(webhookUrl, "markdown", { content: fullContent });
            return {
              id,
              result: {
                content: [
                  {
                    type: "text",
                    text: JSON.stringify({
                      success: true,
                      agent: agent_id ? `${agent_id}:${AGENT_NAMES[agent_id] || ""}` : "default",
                    }),
                  },
                ],
              },
            };
          } catch (err) {
            return { id, error: { code: -32000, message: `发送失败: ${err.message}` } };
          }
        }

        case "wechat_agent_notify": {
          const { agent_id, content } = args;
          if (!agent_id || !content) {
            return { id, error: { code: -32602, message: "缺少必填参数: agent_id, content" } };
          }
          if (!AGENT_NAMES[agent_id]) {
            return { id, error: { code: -32602, message: `无效的 agent_id: ${agent_id}，可用值: 1-4` } };
          }
          const webhookUrl = getWebhookUrl(agent_id);
          if (!webhookUrl) {
            return { id, error: { code: -32000, message: `Agent ${agent_id}(${AGENT_NAMES[agent_id]}) 未配置 webhook URL` } };
          }
          try {
            // agent_notify 在内容前自动加 Agent 名称
            const fullContent = `【${AGENT_NAMES[agent_id]}】\n${content}`;
            const result = await sendQywx(webhookUrl, "text", { content: fullContent });
            return {
              id,
              result: {
                content: [
                  {
                    type: "text",
                    text: JSON.stringify({
                      success: true,
                      agent: `${agent_id}:${AGENT_NAMES[agent_id]}`,
                      text_length: fullContent.length,
                    }),
                  },
                ],
              },
            };
          } catch (err) {
            return { id, error: { code: -32000, message: `发送失败: ${err.message}` } };
          }
        }

        default:
          return { id, error: { code: -32601, message: `未知工具: ${toolName}` } };
      }
    }

    default:
      return { id, error: { code: -32601, message: `未知方法: ${method}` } };
  }
}

// ============ 主循环 ============

async function main() {
  const count = getConfiguredCount();
  console.error(`[wechat] MCP server started. 已配置 ${count} 个机器人`);

  let buffer = "";
  for await (const chunk of process.stdin) {
    buffer += chunk.toString();
    const lines = buffer.split("\n");
    buffer = lines.pop() || "";

    for (const line of lines) {
      const trimmed = line.trim();
      if (!trimmed) continue;

      try {
        const msg = JSON.parse(trimmed);
        const response = await handleMessage(msg);
        if (response) {
          process.stdout.write(JSON.stringify(response) + "\n");
        }
      } catch (err) {
        console.error(`[wechat] parse error: ${err.message}`);
      }
    }
  }
}

main().catch((err) => {
  console.error(`[wechat] fatal: ${err.message}`);
  process.exit(1);
});
