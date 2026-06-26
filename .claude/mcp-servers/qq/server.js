#!/usr/bin/env node
/**
 * QQ MCP Server — 推送通知到 QQ
 *
 * 支持两种方式（按优先级）：
 *   1. PushPlus（推荐） — 通过 pushplus.hxtrip.com API 推送到 QQ
 *   2. QQ邮箱 SMTP      — 通过 QQ邮箱发信触发手机推送
 *
 * 提供工具:
 *   - qq_send_text:      发送纯文本通知到 QQ
 *   - qq_send_report:    发送完整报告到 QQ
 *   - qq_agent_notify:   按 Agent 编号发送（自动带 Agent 名称前缀）
 *
 * 环境变量:
 *   PUSHPLUS_TOKEN       — PushPlus 的推送 token（pushplus.hxtrip.com 注册获取）
 *   QQ_MAIL_USER         — QQ邮箱地址（xxx@qq.com），SMTP 备用方案
 *   QQ_MAIL_PASS         — QQ邮箱 SMTP 授权码
 *   QQ_MAIL_TO           — 接收通知的目标邮箱（默认同 QQ_MAIL_USER）
 *
 * 依赖: 无（仅使用 Node.js 内置 https/http 模块）
 * MCP 协议: stdio JSON-RPC (v2024-11-05)
 */

const https = require("https");
const http = require("http");
const { URL } = require("url");

// ============ 配置 ============

const PUSHPLUS_TOKEN = process.env.PUSHPLUS_TOKEN || "";
const QQ_MAIL_USER = process.env.QQ_MAIL_USER || "";
const QQ_MAIL_PASS = process.env.QQ_MAIL_PASS || "";
const QQ_MAIL_TO = process.env.QQ_MAIL_TO || QQ_MAIL_USER;

// PushPlus API 地址
const PUSHPLUS_API = "http://www.pushplus.plus/send";

// SMTP 配置
const SMTP_HOST = "smtp.qq.com";
const SMTP_PORT = 465;

const AGENT_NAMES = {
  1: "情报员",
  2: "分析师",
  3: "风控官",
  4: "复盘师",
};

/** 检查是否有任何 QQ 通知方式已配置 */
function hasAnyConfig() {
  return !!(PUSHPLUS_TOKEN || (QQ_MAIL_USER && QQ_MAIL_PASS));
}

function getConfiguredCount() {
  let count = 0;
  if (PUSHPLUS_TOKEN) count++;
  if (QQ_MAIL_USER && QQ_MAIL_PASS) count++;
  return count;
}

// ============ 工具定义 ============

const TOOLS = [
  {
    name: "qq_send_text",
    description: "发送纯文本通知到 QQ。支持 PushPlus（推荐）或 QQ邮箱 SMTP 备用。PushPlus 会推送到绑定的 QQ 好友/群。",
    inputSchema: {
      type: "object",
      properties: {
        content: {
          type: "string",
          description: "通知正文内容",
        },
        title: {
          type: "string",
          description: "通知标题（可选，PushPlus 使用）",
          default: "股票投研通知",
        },
      },
      required: ["content"],
    },
  },
  {
    name: "qq_send_report",
    description: "发送完整报告到 QQ（以 HTML 格式展示）。适用于推送完整的分析/风控/复盘报告。",
    inputSchema: {
      type: "object",
      properties: {
        title: {
          type: "string",
          description: "报告标题",
        },
        content: {
          type: "string",
          description: "报告正文（支持 Markdown 格式，PushPlus 会渲染为可读格式）",
        },
        report_type: {
          type: "string",
          description: "报告类型：daily（日报）/ risk（风控）/ review（复盘），用于格式化展示",
          enum: ["daily", "risk", "review"],
          default: "daily",
        },
      },
      required: ["title", "content"],
    },
  },
  {
    name: "qq_agent_notify",
    description: "按 Agent 编号发送 QQ 通知，自动带 Agent 角色名称前缀。适合 Agent 工作流结束时调用。",
    inputSchema: {
      type: "object",
      properties: {
        agent_id: {
          type: "number",
          description: "Agent 编号：1=情报员, 2=分析师, 3=风控官, 4=复盘师",
          enum: [1, 2, 3, 4],
        },
        content: {
          type: "string",
          description: "通知正文内容（不含 Agent 名称，系统自动加前缀）",
        },
        title: {
          type: "string",
          description: "通知标题（可选，默认自动生成）",
        },
      },
      required: ["agent_id", "content"],
    },
  },
];

// ============ PushPlus API 调用 ============

function pushPlusSend(token, title, content, template = "txt") {
  return new Promise((resolve, reject) => {
    const body = JSON.stringify({
      token,
      title,
      content,
      template,
    });

    const url = new URL(PUSHPLUS_API);

    const req = http.request(
      {
        hostname: url.hostname,
        path: url.pathname,
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
            if (parsed.code === 200) {
              resolve(parsed);
            } else {
              reject(new Error(`PushPlus 错误: ${parsed.msg || JSON.stringify(parsed)}`));
            }
          } catch {
            reject(new Error(`PushPlus 响应解析失败: ${data.slice(0, 200)}`));
          }
        });
      }
    );

    req.on("error", reject);
    req.on("timeout", () => {
      req.destroy();
      reject(new Error("PushPlus 请求超时"));
    });
    req.write(body);
    req.end();
  });
}

// ============ QQ邮箱 SMTP 发送 ============

/**
 * 通过 QQ邮箱 SMTP 发送邮件（触发手机推送）
 * 使用 Node.js 内置 TLS 套接字直接实现 SMTP 协议
 */
function sendMailViaSmtp(to, subject, body) {
  return new Promise((resolve, reject) => {
    const net = require("net");
    const tls = require("tls");

    // 先建立 TCP 连接
    const socket = net.connect(SMTP_PORT, SMTP_HOST, () => {
      // 升级到 TLS
      const tlsSocket = tls.connect(
        { socket, servername: SMTP_HOST },
        () => {
          let step = 0;
          let buffer = "";

          function sendCommand(cmd) {
            tlsSocket.write(cmd + "\r\n");
          }

          function onData(chunk) {
            buffer += chunk.toString();

            // 等待完整响应行（以 \r\n 结尾）
            if (!buffer.endsWith("\r\n")) return;

            const line = buffer.trim();
            buffer = "";
            const code = parseInt(line, 10);

            if (code >= 400) {
              tlsSocket.end();
              reject(new Error(`SMTP 错误: ${line}`));
              return;
            }

            step++;
            switch (step) {
              case 1: // 收到 220 欢迎信息
                sendCommand(`EHLO stock-reporter`);
                break;
              case 2: // EHLO 响应
                sendCommand(`AUTH LOGIN`);
                break;
              case 3: // AUTH 请求用户名
                sendCommand(Buffer.from(QQ_MAIL_USER).toString("base64"));
                break;
              case 4: // 用户名 OK，发送密码
                sendCommand(Buffer.from(QQ_MAIL_PASS).toString("base64"));
                break;
              case 5: // 认证成功，发送发件人
                sendCommand(`MAIL FROM:<${QQ_MAIL_USER}>`);
                break;
              case 6: // 发件人 OK
                sendCommand(`RCPT TO:<${to}>`);
                break;
              case 7: // 收件人 OK
                sendCommand("DATA");
                break;
              case 8: // DATA 请求 OK，发送邮件内容
                {
                  const mailBody = [
                    `From: ${QQ_MAIL_USER}`,
                    `To: ${to}`,
                    `Subject: =?UTF-8?B?${Buffer.from(subject).toString("base64")}?=`,
                    "MIME-Version: 1.0",
                    'Content-Type: text/plain; charset="UTF-8"',
                    "Content-Transfer-Encoding: base64",
                    "",
                    Buffer.from(body).toString("base64"),
                    "",
                    ".",
                  ].join("\r\n");
                  tlsSocket.write(mailBody + "\r\n");
                }
                break;
              case 9: // 邮件发送成功
                sendCommand("QUIT");
                break;
              case 10: // 退出
                tlsSocket.end();
                resolve({ success: true, message_id: line });
                break;
            }
          }

          tlsSocket.on("data", onData);
          tlsSocket.on("error", (err) => reject(new Error(`SMTP 连接错误: ${err.message}`)));
        }
      );
    });

    socket.on("error", (err) => reject(new Error(`TCP 连接失败: ${err.message}`)));
  });
}

// ============ 发送逻辑 ============

/**
 * 发送通知——首选 PushPlus，备用 QQ邮箱
 */
async function sendNotification(title, content, template = "txt") {
  const errors = [];

  // 方式一：PushPlus（推荐）
  if (PUSHPLUS_TOKEN) {
    try {
      const result = await pushPlusSend(PUSHPLUS_TOKEN, title, content, template);
      return { channel: "pushplus", success: true, result };
    } catch (err) {
      errors.push(`PushPlus: ${err.message}`);
      console.error(`[qq] PushPlus failed: ${err.message}`);
    }
  }

  // 方式二：QQ邮箱 SMTP（备用）
  if (QQ_MAIL_USER && QQ_MAIL_PASS) {
    try {
      const result = await sendMailViaSmtp(QQ_MAIL_TO, title, content);
      return { channel: "smtp", success: true, result };
    } catch (err) {
      errors.push(`SMTP: ${err.message}`);
      console.error(`[qq] SMTP failed: ${err.message}`);
    }
  }

  // 全部失败
  if (errors.length === 0) {
    throw new Error(
      "未配置任何 QQ 通知方式。\n" +
      "请配置以下至少一项（在 .claude/settings.local.json 的 env 中添加）：\n" +
      "  1. PUSHPLUS_TOKEN — 推荐，去 pushplus.hxtrip.com 注册获取\n" +
      "  2. QQ_MAIL_USER + QQ_MAIL_PASS — QQ邮箱 SMTP 备用方案"
    );
  }

  throw new Error(`所有 QQ 通知方式均失败:\n${errors.join("\n")}`);
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
          serverInfo: { name: "qq-notifier", version: "1.0.0" },
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
      if (!hasAnyConfig()) {
        return {
          id,
          error: {
            code: -32000,
            message:
              "未配置任何 QQ 通知方式。请至少配置以下一项：\n\n" +
              "方法一（推荐）—— PushPlus：\n" +
              "  1. 打开 https://pushplus.hxtrip.com 微信扫码登录\n" +
              "  2. 在「个人中心」获取你的推送 Token\n" +
              "  3. 在「推送配置」中绑定 QQ 好友或 QQ 群\n" +
              "  4. 在 .claude/settings.local.json 中设置 env.PUSHPLUS_TOKEN\n\n" +
              "方法二——QQ邮箱 SMTP：\n" +
              "  1. 登录 QQ邮箱 → 设置 → 账户 → 开启 SMTP 服务\n" +
              "  2. 生成授权码\n" +
              "  3. 在 .claude/settings.local.json 中设置\n" +
              "     env.QQ_MAIL_USER=你的QQ号@qq.com\n" +
              "     env.QQ_MAIL_PASS=你的SMTP授权码",
          },
        };
      }

      switch (toolName) {
        case "qq_send_text": {
          const { content, title = "📊 股票投研通知" } = args;
          if (!content) {
            return { id, error: { code: -32602, message: "缺少必填参数: content" } };
          }
          try {
            const result = await sendNotification(title, content, "txt");
            return {
              id,
              result: {
                content: [
                  {
                    type: "text",
                    text: JSON.stringify({
                      success: true,
                      channel: result.channel,
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

        case "qq_send_report": {
          const { title, content, report_type = "daily" } = args;
          if (!title || !content) {
            return { id, error: { code: -32602, message: "缺少必填参数: title, content" } };
          }
          try {
            // PushPlus 使用 markdown 模板更美观
            const template = "markdown";
            const fullTitle = `📊 ${title}`;
            const result = await sendNotification(fullTitle, content, template);
            return {
              id,
              result: {
                content: [
                  {
                    type: "text",
                    text: JSON.stringify({
                      success: true,
                      channel: result.channel,
                      report_type,
                      content_length: content.length,
                    }),
                  },
                ],
              },
            };
          } catch (err) {
            return { id, error: { code: -32000, message: `发送失败: ${err.message}` } };
          }
        }

        case "qq_agent_notify": {
          const { agent_id, content, title } = args;
          if (!agent_id || !content) {
            return { id, error: { code: -32602, message: "缺少必填参数: agent_id, content" } };
          }
          if (!AGENT_NAMES[agent_id]) {
            return { id, error: { code: -32602, message: `无效的 agent_id: ${agent_id}，可用值: 1-4` } };
          }
          try {
            const agentName = AGENT_NAMES[agent_id];
            const notifyTitle = title || `【${agentName}】股票投研通知 — ${new Date().toISOString().slice(0, 10)}`;
            const notifyContent = `## ${agentName} 通知\n\n${content}`;
            const result = await sendNotification(notifyTitle, notifyContent, "markdown");
            return {
              id,
              result: {
                content: [
                  {
                    type: "text",
                    text: JSON.stringify({
                      success: true,
                      channel: result.channel,
                      agent: `${agent_id}:${agentName}`,
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
  const count = getConfiguredCount();
  const methods = [];
  if (PUSHPLUS_TOKEN) methods.push("PushPlus");
  if (QQ_MAIL_USER && QQ_MAIL_PASS) methods.push("QQ邮箱SMTP");
  console.error(`[qq] MCP server started. 已配置 ${count} 种方式: ${methods.join(", ") || "无"}`);

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
        console.error(`[qq] parse error: ${err.message}`);
      }
    }
  }
}

main().catch((err) => {
  console.error(`[qq] fatal: ${err.message}`);
  process.exit(1);
});
