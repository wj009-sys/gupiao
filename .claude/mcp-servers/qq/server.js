#!/usr/bin/env node
/**
 * QQ通知推送 MCP 服务器
 *
 * 通过 PushPlus API 推送消息到 QQ。
 * 工具: qq_send_text / qq_send_report / qq_agent_notify
 *
 * 使用方式:
 *   1. 在 .claude/settings.local.json 中设置 env.PUSHPLUS_TOKEN
 *   2. Token 从 https://pushplus.hxtrip.com 获取
 *   3. 在 PushPlus 中绑定 QQ好友 或 QQ群
 *
 * 如果 Token 未配置，所有工具会返回提示信息而非报错。
 */
const readline = require("readline");

const PUSHPLUS_API = "https://www.pushplus.plus/send";

function getToken() {
  return process.env.PUSHPLUS_TOKEN || "";
}

/**
 * 通过 PushPlus 推送消息
 */
async function pushPlusSend(title, content, topic = "") {
  const token = getToken();
  if (!token) {
    return {
      success: false,
      message:
        "PUSHPLUS_TOKEN 未配置。请在 .claude/settings.local.json 中设置 env.PUSHPLUS_TOKEN",
    };
  }

  const body = { token, title, content };
  if (topic) body.topic = topic;

  try {
    const resp = await fetch(PUSHPLUS_API, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await resp.json();
    if (data.code === 200) {
      return { success: true, message: "推送成功" };
    }
    return { success: false, message: `PushPlus 返回: ${data.msg || "未知错误"}` };
  } catch (err) {
    return { success: false, message: `推送失败: ${err.message}` };
  }
}

// ─── MCP 协议处理 ──────────────────────────────────────────────

const rl = readline.createInterface({ input: process.stdin });

function sendResponse(id, result) {
  const msg = JSON.stringify({ jsonrpc: "2.0", id, result });
  process.stdout.write(msg + "\n");
}

function sendError(id, code, message) {
  const msg = JSON.stringify({ jsonrpc: "2.0", id, error: { code, message } });
  process.stdout.write(msg + "\n");
}

rl.on("line", async (line) => {
  let msg;
  try {
    msg = JSON.parse(line);
  } catch {
    return;
  }
  const { id, method, params } = msg;

  switch (method) {
    // ── 工具列表 ──
    case "tools/list": {
      sendResponse(id, {
        tools: [
          {
            name: "qq_send_text",
            description: "发送纯文本消息到 QQ（通过 PushPlus）",
            inputSchema: {
              type: "object",
              properties: {
                title: { type: "string", description: "消息标题" },
                content: { type: "string", description: "消息内容" },
              },
              required: ["title", "content"],
            },
          },
          {
            name: "qq_send_report",
            description: "发送报告到 QQ（通过 PushPlus，支持长文本/HTML）",
            inputSchema: {
              type: "object",
              properties: {
                title: { type: "string", description: "报告标题" },
                content: {
                  type: "string",
                  description: "报告内容（纯文本或简易 HTML）",
                },
                template: {
                  type: "string",
                  enum: ["html", "txt"],
                  description: "内容格式（默认 html）",
                },
              },
              required: ["title", "content"],
            },
          },
          {
            name: "qq_agent_notify",
            description:
              "Agent 通知推送到 QQ（统一格式：agent_id + 标题 + 摘要内容）",
            inputSchema: {
              type: "object",
              properties: {
                agent_id: {
                  type: "string",
                  description: "Agent ID（1/2/3/4/5/6/7）",
                },
                title: { type: "string", description: "通知标题" },
                content: { type: "string", description: "通知内容摘要" },
              },
              required: ["agent_id", "title", "content"],
            },
          },
        ],
      });
      break;
    }

    // ── 工具调用 ──
    case "tools/call": {
      const toolName = params?.name;
      const args = params?.arguments || {};

      if (!toolName) {
        sendError(id, -32602, "Missing tool name");
        break;
      }

      try {
        let result;
        switch (toolName) {
          case "qq_send_text": {
            result = await pushPlusSend(
              args.title || "QQ通知",
              args.content || ""
            );
            break;
          }
          case "qq_send_report": {
            const template = args.template || "html";
            result = await pushPlusSend(
              args.title || "报告",
              args.content || "",
              template === "html" ? "html" : ""
            );
            break;
          }
          case "qq_agent_notify": {
            const content = [
              `【Agent${args.agent_id || "?"}】`,
              args.title || "",
              "---",
              args.content || "",
            ].join("\n");
            result = await pushPlusSend(
              `Agent${args.agent_id || "?"} ${args.title || ""}`,
              content
            );
            break;
          }
          default:
            sendError(id, -32601, `Unknown tool: ${toolName}`);
            return;
        }
        sendResponse(id, result);
      } catch (err) {
        sendError(id, -32603, err.message);
      }
      break;
    }

    // ── 初始化 ──
    case "initialize":
    case "initialized": {
      sendResponse(id, {
        protocolVersion: "2024-11-05",
        capabilities: { tools: {} },
        serverInfo: { name: "qq-notifier", version: "1.0.0" },
      });
      break;
    }

    default:
      sendResponse(id, null);
  }
});
