#!/usr/bin/env node
/**
 * WeChat MCP Server — 推送通知到微信
 *
 * 提供工具:
 *   - wechat_send_markdown: 发送 Markdown 消息到企业微信群
 *   - wechat_send_text:     发送纯文本消息
 *   - wechat_send_file:     发送文件（通过消息中的链接）
 *
 * 支持的后端（二选一）:
 *   1. 企业微信群机器人 Webhook — 最稳定，免费无限制（推荐）
 *   2. Server酱 / PushPlus — 个人微信推送（备选）
 *
 * 依赖: 无（仅使用 Node.js 内置 https 模块）
 *
 * 环境变量:
 *   WECHAT_WEBHOOK_URL  — 企业微信群机器人 Webhook URL（推荐）
 *   或
 *   WECHAT_SERVERCHAN_KEY — Server酱 SendKey（备选）
 *   WECHAT_PUSHPLUS_TOKEN — PushPlus Token（备选）
 *
 * 优先级: 企业微信 > Server酱 > PushPlus
 *
 * MCP 协议: stdio JSON-RPC (v2024-11-05)
 */

const https = require("https");
const http = require("http");
const { URL } = require("url");

// ============ 配置 ============

const WEBHOOK_URL = process.env.WECHAT_WEBHOOK_URL || "";
const SERVERCHAN_KEY = process.env.WECHAT_SERVERCHAN_KEY || "";
const PUSHPLUS_TOKEN = process.env.WECHAT_PUSHPLUS_TOKEN || "";

/** 检测当前可用的推送通道 */
function getActiveChannel() {
  if (WEBHOOK_URL) return "qywx";
  if (SERVERCHAN_KEY) return "serverchan";
  if (PUSHPLUS_TOKEN) return "pushplus";
  return null;
}

function getChannelName(ch) {
  const names = { qywx: "企业微信", serverchan: "Server酱", pushplus: "PushPlus" };
  return names[ch] || ch;
}

// ============ 工具定义 ============

const TOOLS = [
  {
    name: "wechat_send_markdown",
    description: "发送 Markdown 格式消息到微信。支持标题、加粗、引用、列表等 Markdown 语法。",
    inputSchema: {
      type: "object",
      properties: {
        title: {
          type: "string",
          description: "消息标题（企业微信会作为首行加粗标题）",
        },
        content: {
          type: "string",
          description: "Markdown 格式的消息正文",
        },
      },
      required: ["title", "content"],
    },
  },
  {
    name: "wechat_send_text",
    description: "发送纯文本消息到微信。不支持格式，适合简短提醒。",
    inputSchema: {
      type: "object",
      properties: {
        content: {
          type: "string",
          description: "文本消息内容",
        },
      },
      required: ["content"],
    },
  },
];

// ============ 各通道的 API 调用 ============

/** 企业微信群机器人 Webhook */
async function sendQywxMarkdown(title, content) {
  const url = new URL(WEBHOOK_URL);
  const fullContent = title ? `## ${title}\n${content}` : content;

  const body = JSON.stringify({
    msgtype: "markdown",
    markdown: { content: fullContent },
  });

  return new Promise((resolve, reject) => {
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

async function sendQywxText(content) {
  const url = new URL(WEBHOOK_URL);
  const body = JSON.stringify({
    msgtype: "text",
    text: { content },
  });

  return new Promise((resolve, reject) => {
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

/** Server酱 */
async function sendServerchan(title, content) {
  const body = JSON.stringify({ title, content, tags: "A股投研" });

  return new Promise((resolve, reject) => {
    const req = https.request(
      {
        hostname: "sct.ftqq.com",
        path: `/${SERVERCHAN_KEY}.send`,
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
            if (parsed.code === 0) resolve(parsed);
            else reject(new Error(`Server酱错误: ${parsed.message} (code=${parsed.code})`));
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

/** PushPlus */
async function sendPushplus(title, content) {
  const body = JSON.stringify({
    token: PUSHPLUS_TOKEN,
    title,
    content,
    template: "markdown",
  });

  return new Promise((resolve, reject) => {
    const req = https.request(
      {
        hostname: "www.pushplus.plus",
        path: "/send",
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
            if (parsed.code === 200) resolve(parsed);
            else reject(new Error(`PushPlus错误: ${parsed.msg} (code=${parsed.code})`));
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

/** 发送 Markdown（自动选择可用通道） */
async function sendMarkdown(title, content) {
  const channel = getActiveChannel();
  switch (channel) {
    case "qywx":
      return sendQywxMarkdown(title, content);
    case "serverchan":
      return sendServerchan(title, content);
    case "pushplus":
      return sendPushplus(title, content);
    default:
      throw new Error("未配置任何微信推送通道。请设置 WECHAT_WEBHOOK_URL、WECHAT_SERVERCHAN_KEY 或 WECHAT_PUSHPLUS_TOKEN");
  }
}

/** 发送纯文本（自动选择可用通道） */
async function sendText(content) {
  const channel = getActiveChannel();
  switch (channel) {
    case "qywx":
      return sendQywxText(content);
    case "serverchan":
      return sendServerchan(content, "");
    case "pushplus":
      return sendPushplus(content, "");
    default:
      throw new Error("未配置任何微信推送通道");
  }
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
          serverInfo: { name: "wechat-notifier", version: "1.0.0" },
        },
      };

    case "notifications/initialized":
      return null;

    case "tools/list":
      return { id, result: { tools: TOOLS } };

    case "tools/call": {
      const toolName = params.name;
      const args = params.arguments || {};

      // 检查是否已配置
      const channel = getActiveChannel();
      if (!channel) {
        return {
          id,
          error: {
            code: -32000,
            message:
              "未配置微信推送通道。请选择以下一种方式配置：\n" +
              "1️⃣ 企业微信群机器人（推荐）: 在 .claude/settings.local.json 中设置 env.WECHAT_WEBHOOK_URL\n" +
              "2️⃣ Server酱: 设置 env.WECHAT_SERVERCHAN_KEY\n" +
              "3️⃣ PushPlus: 设置 env.WECHAT_PUSHPLUS_TOKEN\n\n" +
              "详细教程见 CLAUDE.md",
          },
        };
      }
      console.error(`[wechat] 推送通道: ${getChannelName(channel)}`);

      switch (toolName) {
        case "wechat_send_markdown": {
          const { title, content } = args;
          if (!title || !content) {
            return { id, error: { code: -32602, message: "缺少必填参数: title, content" } };
          }
          try {
            const result = await sendMarkdown(title, content);
            return {
              id,
              result: {
                content: [
                  {
                    type: "text",
                    text: JSON.stringify({
                      success: true,
                      channel: getChannelName(channel),
                      title: title.slice(0, 30),
                    }),
                  },
                ],
              },
            };
          } catch (err) {
            return { id, error: { code: -32000, message: `发送失败: ${err.message}` } };
          }
        }

        case "wechat_send_text": {
          const { content } = args;
          if (!content) {
            return { id, error: { code: -32602, message: "缺少必填参数: content" } };
          }
          try {
            const result = await sendText(content);
            return {
              id,
              result: {
                content: [
                  {
                    type: "text",
                    text: JSON.stringify({
                      success: true,
                      channel: getChannelName(channel),
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
  const ch = getActiveChannel();
  console.error(`[wechat] MCP server started. 通道: ${ch ? getChannelName(ch) : "未配置"}`);

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
