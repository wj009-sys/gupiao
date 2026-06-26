#!/usr/bin/env node
/**
 * Telegram MCP Server — 推送通知到手机
 *
 * 提供工具:
 *   - telegram_send_message: 发送文本消息
 *   - telegram_send_file:    发送文件（报告文档）
 *
 * 依赖: 无（仅使用 Node.js 内置 https 模块）
 *
 * 环境变量:
 *   TELEGRAM_BOT_TOKEN  — BotFather 创建的 Bot Token
 *   TELEGRAM_CHAT_ID    — 接收消息的聊天/频道 ID
 *
 * MCP 协议: stdio JSON-RPC (v2024-11-05)
 */

const https = require("https");
const fs = require("fs");
const path = require("path");

// ============ 配置 ============

const BOT_TOKEN = process.env.TELEGRAM_BOT_TOKEN || "";
const CHAT_ID = process.env.TELEGRAM_CHAT_ID || "";

const API_BASE = `https://api.telegram.org/bot${BOT_TOKEN}`;

// ============ 工具定义 ============

const TOOLS = [
  {
    name: "telegram_send_message",
    description: "发送 Telegram 文本消息到手机。支持 Markdown 格式。",
    inputSchema: {
      type: "object",
      properties: {
        text: {
          type: "string",
          description: "消息内容（纯文本或 Markdown 格式）",
        },
        parse_mode: {
          type: "string",
          description: "解析模式：'Markdown' 或 'HTML'，默认 'Markdown'",
          enum: ["Markdown", "HTML"],
          default: "Markdown",
        },
      },
      required: ["text"],
    },
  },
  {
    name: "telegram_send_file",
    description: "发送文件（如报告文档）到 Telegram。支持发送 markdown 报告、图片等。",
    inputSchema: {
      type: "object",
      properties: {
        file_path: {
          type: "string",
          description: "文件的绝对路径（如 reports/日报/情报/情报摘要_2026-06-26.md）",
        },
        caption: {
          type: "string",
          description: "文件说明文字（可选，会显示在文件上方）",
        },
      },
      required: ["file_path"],
    },
  },
];

// ============ Telegram API 调用 ============

/** POST JSON 到 Telegram Bot API */
function tgPost(method, body) {
  return new Promise((resolve, reject) => {
    const url = new URL(`/bot${BOT_TOKEN}/${method}`, "https://api.telegram.org");
    const data = JSON.stringify(body);

    const req = https.request(
      {
        hostname: "api.telegram.org",
        path: `/bot${BOT_TOKEN}/${method}`,
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Content-Length": Buffer.byteLength(data),
        },
        timeout: 15000,
      },
      (res) => {
        let chunks = "";
        res.on("data", (c) => (chunks += c));
        res.on("end", () => {
          try {
            const parsed = JSON.parse(chunks);
            if (parsed.ok) resolve(parsed);
            else reject(new Error(`Telegram API error: ${parsed.description || JSON.stringify(parsed)}`));
          } catch {
            reject(new Error(`Failed to parse Telegram response: ${chunks.slice(0, 200)}`));
          }
        });
      }
    );

    req.on("error", reject);
    req.on("timeout", () => {
      req.destroy();
      reject(new Error("Telegram API request timed out"));
    });
    req.write(data);
    req.end();
  });
}

/** 使用 multipart/form-data 上传文件到 Telegram */
function tgUploadFile(method, fields, filePath) {
  return new Promise((resolve, reject) => {
    // 构造 multipart 边界
    const boundary = `----FormBoundary${Math.random().toString(36).slice(2)}`;
    const fileContent = fs.readFileSync(filePath);
    const fileName = path.basename(filePath);

    // 构建 multipart body
    const chunks = [];

    // chat_id 字段
    chunks.push(
      Buffer.from(
        `--${boundary}\r\nContent-Disposition: form-data; name="chat_id"\r\n\r\n${CHAT_ID}\r\n`
      )
    );

    // caption 字段（可选）
    if (fields.caption) {
      chunks.push(
        Buffer.from(
          `--${boundary}\r\nContent-Disposition: form-data; name="caption"\r\n\r\n${fields.caption}\r\n`
        )
      );
    }

    // 文件字段
    chunks.push(
      Buffer.from(
        `--${boundary}\r\nContent-Disposition: form-data; name="document"; filename="${fileName}"\r\nContent-Type: application/octet-stream\r\n\r\n`
      )
    );
    chunks.push(fileContent);
    chunks.push(Buffer.from(`\r\n--${boundary}--\r\n`));

    const body = Buffer.concat(chunks);

    const req = https.request(
      {
        hostname: "api.telegram.org",
        path: `/bot${BOT_TOKEN}/${method}`,
        method: "POST",
        headers: {
          "Content-Type": `multipart/form-data; boundary=${boundary}`,
          "Content-Length": body.length,
        },
        timeout: 60000,
      },
      (res) => {
        let data = "";
        res.on("data", (c) => (data += c));
        res.on("end", () => {
          try {
            const parsed = JSON.parse(data);
            if (parsed.ok) resolve(parsed);
            else reject(new Error(`Telegram upload error: ${parsed.description}`));
          } catch {
            reject(new Error(`Failed to parse upload response: ${data.slice(0, 200)}`));
          }
        });
      }
    );

    req.on("error", reject);
    req.on("timeout", () => {
      req.destroy();
      reject(new Error("Telegram upload timed out"));
    });
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
          serverInfo: { name: "telegram-notifier", version: "1.0.0" },
        },
      };

    case "notifications/initialized":
      return null;

    case "tools/list":
      return { id, result: { tools: TOOLS } };

    case "tools/call": {
      const toolName = params.name;
      const args = params.arguments || {};

      // 检查 token/chat_id 是否配置
      if (!BOT_TOKEN) {
        return {
          id,
          error: {
            code: -32000,
            message:
              "TELEGRAM_BOT_TOKEN 未配置。请在 .claude/settings.local.json 中设置 env.TELEGRAM_BOT_TOKEN。\n如何获取: 在 Telegram 中 @BotFather → /newbot → 获取 token。",
          },
        };
      }
      if (!CHAT_ID) {
        return {
          id,
          error: {
            code: -32000,
            message:
              "TELEGRAM_CHAT_ID 未配置。请在 .claude/settings.local.json 中设置 env.TELEGRAM_CHAT_ID。\n如何获取: 给 @userinfobot 发任意消息获取你的 Chat ID。",
          },
        };
      }

      switch (toolName) {
        case "telegram_send_message": {
          const { text, parse_mode = "Markdown" } = args;
          if (!text) {
            return { id, error: { code: -32602, message: "缺少必填参数: text" } };
          }
          try {
            const result = await tgPost("sendMessage", {
              chat_id: CHAT_ID,
              text,
              parse_mode,
              disable_web_page_preview: false,
            });
            return {
              id,
              result: {
                content: [
                  {
                    type: "text",
                    text: JSON.stringify({
                      success: true,
                      message_id: result.result?.message_id,
                      text_length: text.length,
                    }),
                  },
                ],
              },
            };
          } catch (err) {
            return { id, error: { code: -32000, message: `发送消息失败: ${err.message}` } };
          }
        }

        case "telegram_send_file": {
          const { file_path: filePath, caption = "" } = args;
          if (!filePath) {
            return { id, error: { code: -32602, message: "缺少必填参数: file_path" } };
          }
          // 解析相对路径（相对于项目根目录）
          const absPath = path.isAbsolute(filePath)
            ? filePath
            : path.resolve(process.cwd(), filePath);

          if (!fs.existsSync(absPath)) {
            return {
              id,
              error: { code: -32602, message: `文件不存在: ${absPath}` },
            };
          }

          try {
            const result = await tgUploadFile("sendDocument", { caption }, absPath);
            return {
              id,
              result: {
                content: [
                  {
                    type: "text",
                    text: JSON.stringify({
                      success: true,
                      message_id: result.result?.message_id,
                      file_name: path.basename(absPath),
                    }),
                  },
                ],
              },
            };
          } catch (err) {
            return { id, error: { code: -32000, message: `发送文件失败: ${err.message}` } };
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
  console.error(`[telegram] MCP server started. Bot: @${BOT_TOKEN ? BOT_TOKEN.slice(0, 8) + "..." : "未配置"}`);

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
        console.error(`[telegram] parse error: ${err.message}`);
      }
    }
  }
}

main().catch((err) => {
  console.error(`[telegram] fatal: ${err.message}`);
  process.exit(1);
});
