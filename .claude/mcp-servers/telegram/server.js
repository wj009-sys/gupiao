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
const http = require("http");
const net = require("net");
const tls = require("tls");
const fs = require("fs");
const path = require("path");
const { URL } = require("url");

// ============ 配置 ============

const BOT_TOKEN = process.env.TELEGRAM_BOT_TOKEN || "";
const CHAT_ID = process.env.TELEGRAM_CHAT_ID || "";
const SOCKS_PROXY = process.env.SOCKS_PROXY || process.env.HTTPS_PROXY || "";

const API_HOST = "api.telegram.org";
const API_BASE = `https://${API_HOST}/bot${BOT_TOKEN}`;

// Telegram 长轮询状态
let tgUpdateOffset = 0;
let tgUpdateFile = path.join(__dirname, ".telegram_offset");

// ============ SOCKS5 代理支持 ============

/** 从 socket 读取精确字节数 */
function readExact(socket, n) {
  return new Promise((resolve, reject) => {
    const buf = [];
    let len = 0;
    const onData = (chunk) => {
      buf.push(chunk);
      len += chunk.length;
      if (len >= n) {
        socket.removeListener("data", onData);
        resolve(Buffer.concat(buf));
      }
    };
    socket.on("data", onData);
    socket.on("error", reject);
  });
}

/** 通过 SOCKS5 代理建立 TLS 连接到目标主机 */
function socks5Connect(host, port) {
  return new Promise((resolve, reject) => {
    if (!SOCKS_PROXY) {
      const socket = net.connect(port, host, () => resolve(socket));
      socket.on("error", reject);
      return;
    }

    let proxyHost, proxyPort;
    if (SOCKS_PROXY.startsWith("socks5://")) {
      const u = new URL(SOCKS_PROXY);
      proxyHost = u.hostname;
      proxyPort = parseInt(u.port, 10) || 10808;
    } else {
      proxyHost = "127.0.0.1";
      proxyPort = parseInt(SOCKS_PROXY, 10) || 10808;
    }

    const socket = net.connect(proxyPort, proxyHost, async () => {
      try {
        // 第1步: 认证协商
        socket.write(Buffer.from([0x05, 0x01, 0x00]));
        const authResp = await readExact(socket, 2);
        if (authResp[0] !== 0x05 || authResp[1] !== 0x00) {
          throw new Error(`SOCKS5 握手失败: ${authResp[1]}`);
        }

        // 第2步: 连接请求
        const hostParts = host.split(".");
        let addr;
        if (hostParts.length === 4 && hostParts.every((p) => !isNaN(p) && p >= 0 && p <= 255)) {
          addr = Buffer.concat([Buffer.from([0x01]), Buffer.from(hostParts.map(Number))]);
        } else {
          const domainBuf = Buffer.from(host, "utf8");
          addr = Buffer.concat([Buffer.from([0x03, domainBuf.length]), domainBuf]);
        }
        const portBuf = Buffer.alloc(2);
        portBuf.writeUInt16BE(port);
        socket.write(Buffer.concat([Buffer.from([0x05, 0x01, 0x00]), addr, portBuf]));

        // 读取响应: VER+REP+RSV+ATYPE = 4字节，然后根据ATYPE读取地址
        const header = await readExact(socket, 4);
        if (header[0] !== 0x05 || header[1] !== 0x00) {
          throw new Error(`SOCKS5 连接拒绝: ${header[1]}`);
        }
        let addrLen;
        switch (header[3]) {
          case 0x01: addrLen = 4; break;  // IPv4
          case 0x03: const lenByte = (await readExact(socket, 1))[0]; addrLen = lenByte; break; // 域名
          case 0x04: addrLen = 16; break; // IPv6
          default: throw new Error(`SOCKS5 未知地址类型: ${header[3]}`);
        }
        await readExact(socket, addrLen + 2); // 地址 + 端口

        // 升级到 TLS
        const tlsSocket = tls.connect({ socket, servername: host, host, port });
        tlsSocket.on("ready", () => resolve(tlsSocket));
        tlsSocket.on("error", reject);
      } catch (err) {
        socket.destroy();
        reject(err);
      }
    });
    socket.on("error", reject);
  });
}

/** HTTP 代理 CONNECT 隧道 */
function httpConnectTunnel(host, port, proxyHost, proxyPort) {
  return new Promise((resolve, reject) => {
    const socket = net.connect(proxyPort, proxyHost, () => {
      socket.write(`CONNECT ${host}:${port} HTTP/1.1\r\nHost: ${host}:${port}\r\n\r\n`);
    });
    let buf = "";
    socket.on("data", (chunk) => {
      buf += chunk.toString();
      if (buf.includes("\r\n\r\n")) {
        if (buf.startsWith("HTTP/1.1 200") || buf.startsWith("HTTP/1.0 200")) {
          const tlsSocket = tls.connect({ socket, servername: host, host, port });
          tlsSocket.on("ready", () => resolve(tlsSocket));
          tlsSocket.on("error", reject);
        } else {
          reject(new Error(`HTTP CONNECT 失败: ${buf.slice(0, 100)}`));
        }
      }
    });
    socket.on("error", reject);
  });
}

/** 通过代理或直连发送 HTTPS 请求 */
function requestWithProxy(options, body) {
  return new Promise((resolve, reject) => {
    if (SOCKS_PROXY) {
      // 通过 SOCKS5/HTTP 代理
      socks5Connect(API_HOST, 443)
        .then((tlsSocket) => {
          const req = https.request({ ...options, createConnection: () => tlsSocket, socket: tlsSocket, agent: false }, resolve);
          req.on("error", reject);
          if (body) req.write(body);
          req.end();
        })
        .catch(reject);
    } else {
      // 直连
      const req = https.request(options, resolve);
      req.on("error", reject);
      if (body) req.write(body);
      req.end();
    }
  });
}

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
  {
    name: "telegram_listen",
    description: "监听 Telegram 收到的消息（交互模式）。启动监听后，在 Telegram 中给机器人发消息，系统会检测到并返回消息内容和发送者 Chat ID。支持识别 /指令 格式的命令。",
    inputSchema: {
      type: "object",
      properties: {
        duration: {
          type: "number",
          description: "监听时长（秒），默认 120，最长 600",
          default: 120,
        },
      },
    },
  },
];

// ============ Telegram API 调用 ============

/** POST JSON 到 Telegram Bot API（支持代理） */
function tgPost(method, body) {
  return new Promise((resolve, reject) => {
    const data = JSON.stringify(body);
    requestWithProxy(
      {
        hostname: API_HOST,
        path: `/bot${BOT_TOKEN}/${method}`,
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Content-Length": Buffer.byteLength(data),
        },
        timeout: 15000,
      },
      data
    )
      .then((res) => {
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
      })
      .catch(reject);
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

    requestWithProxy(
      {
        hostname: API_HOST,
        path: `/bot${BOT_TOKEN}/${method}`,
        method: "POST",
        headers: {
          "Content-Type": `multipart/form-data; boundary=${boundary}`,
          "Content-Length": body.length,
        },
        timeout: 60000,
      },
      body
    )
      .then((res) => {
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
      })
      .catch(reject);
  });
}

// ============ Telegram 长轮询（交互监听） ============

/** 持久化保存 offset */
function saveOffset() {
  try { fs.writeFileSync(tgUpdateFile, String(tgUpdateOffset), "utf8"); } catch {}
}

/** 加载持久化的 offset */
function loadOffset() {
  try {
    const d = fs.readFileSync(tgUpdateFile, "utf8").trim();
    tgUpdateOffset = parseInt(d, 10) || 0;
  } catch { tgUpdateOffset = 0; }
}

/** 轮询 Telegram 获取新消息 */
function tgGetUpdates(timeout) {
  return new Promise((resolve, reject) => {
    loadOffset();
    const params = new URLSearchParams({ offset: tgUpdateOffset + 1, timeout: String(timeout), allowed_updates: '["message"]' });
    requestWithProxy({
      hostname: API_HOST,
      path: `/bot${BOT_TOKEN}/getUpdates?${params}`,
      method: "GET",
      timeout: (timeout + 5) * 1000,
    })
      .then((res) => {
        let data = "";
        res.on("data", (c) => (data += c));
        res.on("end", () => {
          try {
            const parsed = JSON.parse(data);
            if (!parsed.ok) return reject(new Error(`Telegram API: ${parsed.description}`));
            const updates = parsed.result || [];
            // 更新 offset
            for (const u of updates) {
              if (u.update_id > tgUpdateOffset) tgUpdateOffset = u.update_id;
            }
            saveOffset();
            resolve(updates);
          } catch (e) {
            reject(new Error(`解析 getUpdates 响应失败: ${e.message}`));
          }
        });
      })
      .catch(reject);
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

        case "telegram_listen": {
          const duration = Math.min(args.duration || 120, 600);
          console.error(`[telegram] 📡 监听模式启动（${duration}秒）`);
          console.error(`[telegram] 📡 给机器人发消息，系统会检测到`);

          const startTime = Date.now();
          const detectedMessages = [];

          return new Promise((resolve) => {
            const pollInterval = setInterval(async () => {
              try {
                const updates = await tgGetUpdates(5);
                for (const u of updates) {
                  const msg = u.message;
                  if (!msg || !msg.text) continue;
                  const chat = msg.chat || {};
                  detectedMessages.push({
                    update_id: u.update_id,
                    text: msg.text,
                    chat_id: chat.id,
                    chat_name: chat.first_name || chat.title || "",
                    date: msg.date,
                    is_command: msg.text.startsWith("/"),
                    from_id: msg.from?.id,
                    from_name: msg.from?.first_name || "",
                  });
                }
              } catch {}
            }, 3000);

            const timeout = setTimeout(() => {
              clearInterval(pollInterval);
              const result = {
                success: true,
                duration_seconds: Math.round((Date.now() - startTime) / 1000),
                messages_detected: detectedMessages.length,
                messages: detectedMessages,
                tip: detectedMessages.length > 0
                  ? "检测到消息！可以用 telegram_send_message 回复。如果是 /指令 格式的命令，可以触发对应的 Agent 技能。"
                  : `${Math.round((Date.now() - startTime) / 1000)}秒监听结束，未检测到新消息。请在 Telegram 中给 @Qby0001bot 发消息。`,
              };
              resolve({
                id,
                result: {
                  content: [{ type: "text", text: JSON.stringify(result) }],
                },
              });
            }, duration * 1000);

            // 如果 MCP 请求被取消，清理定时器
            if (id.cancel) {
              id.cancel.then(() => {
                clearInterval(pollInterval);
                clearTimeout(timeout);
              });
            }
          });
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
