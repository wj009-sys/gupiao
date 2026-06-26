#!/usr/bin/env node
/**
 * QQ Bot MCP Server — 通过QQ官方机器人API推送通知到QQ
 *
 * 使用 QQ 开放平台官方 API v2：
 *   - WebSocket 连接到 QQ Gateway（保持机器人在线）
 *   - REST API 发送消息（单聊/群聊）
 *
 * 提供工具:
 *   - qqbot_send_text:      发送文本消息到QQ
 *   - qqbot_send_markdown:  发送Markdown消息到QQ
 *   - qqbot_agent_notify:   按 Agent 编号发送（自动带角色名称前缀）
 *   - qqbot_listen:         启动事件监听模式（获取用户 OpenID）
 *
 * 环境变量（在 .claude/settings.local.json 的 env 中配置）：
 *   QQBOT_APP_ID            — QQ机器人 AppID（必填）
 *   QQBOT_APP_SECRET        — QQ机器人 AppSecret（必填）
 *   QQBOT_TARGET_OPENID     — 目标用户 OpenID（推送对象）
 *   QQBOT_TARGET_GROUP      — 目标群 GroupOpenID（与上面二选一）
 *
 * 依赖: 无（仅使用 Node.js 内置模块）
 * MCP 协议: stdio JSON-RPC (v2024-11-05)
 */

const https = require("https");
const http = require("http");
const { URL } = require("url");

// ============ 配置 ============

const APP_ID = process.env.QQBOT_APP_ID || "";
const APP_SECRET = process.env.QQBOT_APP_SECRET || "";
const TARGET_OPENID = process.env.QQBOT_TARGET_OPENID || "";
const TARGET_GROUP = process.env.QQBOT_TARGET_GROUP || "";

const AGENT_NAMES = {
  1: "情报员",
  2: "分析师",
  3: "风控官",
  4: "复盘师",
};

// ============ QQ Bot 状态 ============

let botState = {
  accessToken: "",
  tokenExpiresAt: 0,
  ws: null,
  ready: false,
  readyPromise: null,
  resolveReady: null,
  sessionId: "",
  heartbeatInterval: null,
  lastSeq: null,
  reconnectAttempts: 0,
  maxReconnectAttempts: 5,
  eventLog: [],
  shard: { shard_id: 0, num_shards: 1 },
};

// ============ 工具定义 ============

const TOOLS = [
  {
    name: "qqbot_send_text",
    description: "发送文本消息到QQ（单聊或群聊）。需要先在 settings 中配置 QQBOT_TARGET_OPENID 或 QQBOT_TARGET_GROUP。",
    inputSchema: {
      type: "object",
      properties: {
        content: {
          type: "string",
          description: "消息正文",
        },
        target_type: {
          type: "string",
          description: "目标类型：user（单聊）/ group（群聊），默认自动检测配置",
          enum: ["user", "group"],
        },
      },
      required: ["content"],
    },
  },
  {
    name: "qqbot_send_markdown",
    description: "发送 Markdown 格式消息到QQ。",
    inputSchema: {
      type: "object",
      properties: {
        title: {
          type: "string",
          description: "标题",
        },
        content: {
          type: "string",
          description: "Markdown 正文",
        },
        target_type: {
          type: "string",
          description: "目标类型：user（单聊）/ group（群聊），默认自动检测配置",
          enum: ["user", "group"],
        },
      },
      required: ["title", "content"],
    },
  },
  {
    name: "qqbot_agent_notify",
    description: "按 Agent 编号发送 QQ 通知，自动带 Agent 角色名称前缀。",
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
      },
      required: ["agent_id", "content"],
    },
  },
  {
    name: "qqbot_listen",
    description: "启动事件监听模式。监听QQ机器人收到的事件并记录到日志，用于获取用户的 OpenID。加机器人好友后发一条消息，系统会记录 OpenID。",
    inputSchema: {
      type: "object",
      properties: {
        duration: {
          type: "number",
          description: "监听时长（秒），默认 120 秒",
          default: 120,
        },
      },
    },
  },
];

// ============ HTTP 请求工具 ============

function httpRequest(options, body = null) {
  return new Promise((resolve, reject) => {
    const isHttps = options.protocol !== "http:";
    const mod = isHttps ? https : http;

    const req = mod.request(
      {
        hostname: options.hostname,
        path: options.path,
        method: options.method || "GET",
        headers: options.headers || { "Content-Type": "application/json" },
        timeout: options.timeout || 15000,
      },
      (res) => {
        let data = "";
        res.on("data", (c) => (data += c));
        res.on("end", () => {
          try {
            const parsed = JSON.parse(data);
            resolve(parsed);
          } catch {
            reject(new Error(`HTTP响应解析失败: ${data.slice(0, 200)}`));
          }
        });
      }
    );
    req.on("error", reject);
    req.on("timeout", () => {
      req.destroy();
      reject(new Error("请求超时"));
    });
    if (body) req.write(typeof body === "string" ? body : JSON.stringify(body));
    req.end();
  });
}

// ============ QQ Bot API ============

/** 获取 Access Token */
async function getAccessToken() {
  const result = await httpRequest(
    {
      hostname: "bots.qq.com",
      path: "/app/getAppAccessToken",
      method: "POST",
      headers: { "Content-Type": "application/json" },
      timeout: 10000,
    },
    { appId: APP_ID, clientSecret: APP_SECRET }
  );

  if (result.access_token) {
    botState.accessToken = result.access_token;
    botState.tokenExpiresAt = Date.now() + (result.expires_in || 7200) * 1000;
    console.error(`[qqbot] Token acquired, expires in ${result.expires_in || 7200}s`);
    return result.access_token;
  }
  throw new Error(`获取Token失败: ${JSON.stringify(result)}`);
}

/** 获取 WebSocket Gateway URL */
async function getGateway() {
  const result = await httpRequest({
    hostname: "api.sgroup.qq.com",
    path: "/gateway/bot",
    method: "GET",
    headers: {
      Authorization: `QQBot ${botState.accessToken}`,
    },
    timeout: 10000,
  });

  if (result.url) {
    console.error(`[qqbot] Gateway URL obtained`);
    return result.url;
  }
  throw new Error(`获取Gateway失败: ${JSON.stringify(result)}`);
}

/** 发送消息到用户 */
async function sendToUser(openid, content, msgType = 0, markdown = null) {
  const body = { msg_type: msgType };
  if (msgType === 0) {
    body.content = content;
  } else if (msgType === 2 && markdown) {
    body.markdown = markdown;
  }

  const result = await httpRequest(
    {
      hostname: "api.sgroup.qq.com",
      path: `/v2/users/${openid}/messages`,
      method: "POST",
      headers: {
        Authorization: `QQBot ${botState.accessToken}`,
        "Content-Type": "application/json",
        "X-Union-Appid": APP_ID,
      },
      timeout: 10000,
    },
    body
  );

  if (result.id || result.message_id) return result;
  // 某些错误码可能仍返回200但包含错误
  if (result.code) throw new Error(`QQ API错误: ${result.message || JSON.stringify(result)}`);
  return result;
}

/** 发送消息到群 */
async function sendToGroup(groupOpenid, content, msgType = 0, markdown = null) {
  const body = { msg_type: msgType };
  if (msgType === 0) {
    body.content = content;
  } else if (msgType === 2 && markdown) {
    body.markdown = markdown;
  }

  const result = await httpRequest(
    {
      hostname: "api.sgroup.qq.com",
      path: `/v2/groups/${groupOpenid}/messages`,
      method: "POST",
      headers: {
        Authorization: `QQBot ${botState.accessToken}`,
        "Content-Type": "application/json",
        "X-Union-Appid": APP_ID,
      },
      timeout: 10000,
    },
    body
  );

  if (result.id || result.message_id) return result;
  if (result.code) throw new Error(`QQ API错误: ${result.message || JSON.stringify(result)}`);
  return result;
}

// ============ WebSocket 生命周期 ============

/** 启动 WebSocket 连接 */
async function connectWebSocket() {
  // 确保有有效的 Access Token
  if (!botState.accessToken || Date.now() >= botState.tokenExpiresAt - 300000) {
    await getAccessToken();
  }

  // 获取 Gateway URL
  const gatewayUrl = await getGateway();
  const wsUrl = `${gatewayUrl}?v=1&encoding=json`;

  console.error(`[qqbot] Connecting to Gateway...`);

  return new Promise((resolve, reject) => {
    let resolved = false;
    const ws = new WebSocket(wsUrl);
    botState.ws = ws;

    ws.onopen = () => {
      console.error(`[qqbot] WebSocket connected`);
    };

    ws.onmessage = (event) => {
      try {
        const payload = JSON.parse(event.data);
        handleWsPayload(payload);
      } catch (err) {
        console.error(`[qqbot] WS message parse error: ${err.message}`);
      }
    };

    ws.onclose = (event) => {
      console.error(`[qqbot] WebSocket closed: code=${event.code}`);
      botState.ready = false;
      stopHeartbeat();
      if (!resolved) {
        resolved = true;
        reject(new Error(`WebSocket closed unexpectedly: ${event.code}`));
      }
      // 自动重连
      scheduleReconnect();
    };

    ws.onerror = (event) => {
      console.error(`[qqbot] WebSocket error`);
      if (!resolved) {
        resolved = true;
        reject(new Error("WebSocket connection failed"));
      }
    };

    // 设置一个总的连接超时
    setTimeout(() => {
      if (!resolved) {
        resolved = true;
        reject(new Error("WebSocket connection timeout (15s)"));
      }
    }, 15000);
  });
}

/** 处理 WebSocket 消息 */
function handleWsPayload(payload) {
  const { op, d, s, t } = payload;

  switch (op) {
    case 0: // Dispatch (事件分发)
      botState.lastSeq = s;
      handleDispatch(t, d);
      break;

    case 7: // Reconnect
      console.error(`[qqbot] Gateway requested reconnect`);
      reconnect();
      break;

    case 9: // Invalid Session
      console.error(`[qqbot] Invalid session, re-identifying...`);
      // 如果是无效session，重新identify（不分片则不需要resume）
      identify();
      break;

    case 10: // Hello
      console.error(`[qqbot] Received Hello, heartbeat_interval=${d.heartbeat_interval}`);
      startHeartbeat(d.heartbeat_interval);
      // 发送 Identify
      identify();
      break;

    case 11: // Heartbeat ACK
      console.error(`[qqbot] Heartbeat ACK`);
      break;
  }
}

/** 处理事件分发 */
function handleDispatch(eventType, data) {
  switch (eventType) {
    case "READY":
      botState.sessionId = data.session_id;
      botState.ready = true;
      botState.reconnectAttempts = 0;
      if (botState.resolveReady) botState.resolveReady();
      console.error(`[qqbot] Ready! Session: ${data.session_id}`);
      // 打印用户信息
      if (data.user) {
        console.error(`[qqbot] Bot info: id=${data.user.id}, name=${data.user.username}`);
      }
      break;

    case "RESUMED":
      botState.ready = true;
      console.error(`[qqbot] Session resumed`);
      break;

    // 记录事件用于监听模式
    case "C2C_MESSAGE_CREATE":
    case "AT_MESSAGE_CREATE":
    case "FRIEND_ADD":
      {
        const event = { type: eventType, data, time: new Date().toISOString() };
        botState.eventLog.push(event);
        const openid = data.author?.user_openid || data.author?.id || "?";
        const content = data.content || "";
        console.error(`[qqbot] 📩 Event: ${eventType} from=${openid} content="${content.slice(0, 50)}"`);
      }
      break;
  }
}

/** 发送 Identify */
function identify() {
  if (!botState.ws || botState.ws.readyState !== WebSocket.OPEN) return;

  const identifyPayload = {
    op: 2,
    d: {
      token: `QQBot ${botState.accessToken}`,
      intents: 1 << 25 | 1 << 30, // C2C_MESSAGE + FRIEND
      shard: [botState.shard.shard_id, botState.shard.num_shards],
    },
  };

  // 如果有session_id且是重连，使用resume
  if (botState.sessionId) {
    identifyPayload.op = 6;
    identifyPayload.d = {
      token: `QQBot ${botState.accessToken}`,
      session_id: botState.sessionId,
      seq: botState.lastSeq,
    };
  }

  botState.ws.send(JSON.stringify(identifyPayload));
  console.error(`[qqbot] Sent ${botState.sessionId ? 'Resume' : 'Identify'}`);
}

/** 启动心跳 */
function startHeartbeat(interval) {
  stopHeartbeat();
  // jitter: 0~1000ms 随机延迟避免精确同步
  const jitter = Math.floor(Math.random() * 1000);

  setTimeout(() => {
    sendHeartbeat();
    botState.heartbeatInterval = setInterval(sendHeartbeat, interval);
    console.error(`[qqbot] Heartbeat started (interval: ${interval}ms)`);
  }, interval + jitter);
}

/** 发送心跳 */
function sendHeartbeat() {
  if (botState.ws && botState.ws.readyState === WebSocket.OPEN) {
    botState.ws.send(JSON.stringify({ op: 1, d: botState.lastSeq }));
  }
}

/** 停止心跳 */
function stopHeartbeat() {
  if (botState.heartbeatInterval) {
    clearInterval(botState.heartbeatInterval);
    botState.heartbeatInterval = null;
  }
}

/** 重连调度 */
function scheduleReconnect() {
  if (botState.reconnectAttempts >= botState.maxReconnectAttempts) {
    console.error(`[qqbot] Max reconnect attempts (${botState.maxReconnectAttempts}) reached, giving up`);
    return;
  }

  const delay = Math.min(1000 * Math.pow(2, botState.reconnectAttempts), 30000);
  botState.reconnectAttempts++;
  console.error(`[qqbot] Reconnecting in ${delay}ms (attempt ${botState.reconnectAttempts})`);

  setTimeout(() => reconnect(), delay);
}

/** 执行重连 */
async function reconnect() {
  try {
    await connectWebSocket();
  } catch (err) {
    console.error(`[qqbot] Reconnect failed: ${err.message}`);
    scheduleReconnect();
  }
}

/** 等待机器人就绪 */
async function waitForReady(timeout = 30000) {
  if (botState.ready) return;

  return new Promise((resolve, reject) => {
    botState.resolveReady = resolve;

    setTimeout(() => {
      if (!botState.ready) {
        reject(new Error("QQ Bot 连接超时（30s）"));
      }
    }, timeout);
  });
}

/** 初始化 QQ Bot 连接 */
async function initBot() {
  if (!APP_ID || !APP_SECRET) {
    console.error(`[qqbot] QQ Bot 未配置（缺少 QQBOT_APP_ID 或 QQBOT_APP_SECRET）`);
    return false;
  }

  try {
    await connectWebSocket();
    return true;
  } catch (err) {
    console.error(`[qqbot] Initial connection failed: ${err.message}`);
    // 尝试重连
    scheduleReconnect();
    return false;
  }
}

// ============ 消息发送 ============

async function sendMessage(content, msgType = 0, markdown = null, targetType = null) {
  // 等待机器人就绪
  if (!botState.ready) {
    try {
      await waitForReady(20000);
    } catch {
      throw new Error("QQ Bot 尚未就绪，无法发送消息。请检查 Token 和网络连接。");
    }
  }

  const type = targetType || (TARGET_GROUP ? "group" : TARGET_OPENID ? "user" : null);
  if (!type) {
    throw new Error(
      "未配置推送目标。请在 .claude/settings.local.json 中设置：\n" +
      "  QQBOT_TARGET_OPENID  (单聊，获取方法：配好AppID+Secret后调用 qqbot_listen，加机器人好友发一条消息)\n" +
      "  或 QQBOT_TARGET_GROUP (群聊)"
    );
  }

  if (type === "user" && TARGET_OPENID) {
    return await sendToUser(TARGET_OPENID, content, msgType, markdown);
  } else if (type === "group" && TARGET_GROUP) {
    return await sendToGroup(TARGET_GROUP, content, msgType, markdown);
  } else {
    throw new Error(`目标类型 "${type}" 未配置对应的 ID`);
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
          serverInfo: { name: "qqbot-notifier", version: "1.0.0" },
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
      if (!APP_ID || !APP_SECRET) {
        return {
          id,
          error: {
            code: -32000,
            message:
              "QQ Bot 未完全配置。请在 .claude/settings.local.json 中设置：\n" +
              "  env.QQBOT_APP_ID = 你的机器人 AppID\n" +
              "  env.QQBOT_APP_SECRET = 你的机器人 AppSecret\n\n" +
              "创建机器人请访问: https://q.qq.com",
          },
        };
      }

      switch (toolName) {
        case "qqbot_send_text": {
          const { content, target_type } = args;
          if (!content) {
            return { id, error: { code: -32602, message: "缺少必填参数: content" } };
          }
          try {
            const result = await sendMessage(content, 0, null, target_type);
            return {
              id,
              result: {
                content: [
                  {
                    type: "text",
                    text: JSON.stringify({
                      success: true,
                      message_id: result.id || result.message_id,
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

        case "qqbot_send_markdown": {
          const { title, content, target_type } = args;
          if (!title || !content) {
            return { id, error: { code: -32602, message: "缺少必填参数: title, content" } };
          }
          try {
            const markdownContent = `## ${title}\n${content}`;
            const result = await sendMessage("", 2, { content: markdownContent }, target_type);
            return {
              id,
              result: {
                content: [
                  {
                    type: "text",
                    text: JSON.stringify({
                      success: true,
                      message_id: result.id || result.message_id,
                    }),
                  },
                ],
              },
            };
          } catch (err) {
            return { id, error: { code: -32000, message: `发送失败: ${err.message}` } };
          }
        }

        case "qqbot_agent_notify": {
          const { agent_id, content } = args;
          if (!agent_id || !content) {
            return { id, error: { code: -32602, message: "缺少必填参数: agent_id, content" } };
          }
          if (!AGENT_NAMES[agent_id]) {
            return { id, error: { code: -32602, message: `无效的 agent_id: ${agent_id}，可用值: 1-4` } };
          }
          try {
            const agentName = AGENT_NAMES[agent_id];
            const notifyContent = `【${agentName}】\n${content}`;
            const result = await sendMessage(notifyContent, 0, null);
            return {
              id,
              result: {
                content: [
                  {
                    type: "text",
                    text: JSON.stringify({
                      success: true,
                      agent: `${agent_id}:${agentName}`,
                      text_length: notifyContent.length,
                    }),
                  },
                ],
              },
            };
          } catch (err) {
            return { id, error: { code: -32000, message: `发送失败: ${err.message}` } };
          }
        }

        case "qqbot_listen": {
          const duration = Math.min(args.duration || 120, 600);
          console.error(`[qqbot] 📡 事件监听模式启动（${duration}秒）`);
          console.error(`[qqbot] 📡 请用手机QQ加机器人好友后发一条消息`);

          const eventCount = botState.eventLog.length;

          return new Promise((resolve) => {
            const checkInterval = setInterval(() => {
              if (botState.eventLog.length > eventCount) {
                clearInterval(checkInterval);
                clearTimeout(timeout);

                const events = botState.eventLog.slice(eventCount);
                const openids = [...new Set(
                  events
                    .map(e => e.data?.author?.user_openid || e.data?.author?.id)
                    .filter(Boolean)
                )];

                resolve({
                  id,
                  result: {
                    content: [
                      {
                        type: "text",
                        text: JSON.stringify({
                          success: true,
                          events_detected: events.length,
                          openids_found: openids,
                          message: openids.length > 0
                            ? `检测到用户OpenID：${openids[0]}。请在 settings.local.json 中设置 QQBOT_TARGET_OPENID。`
                            : "未检测到OpenID，请确保机器人已添加好友并发送了消息",
                        }),
                      },
                    ],
                  },
                });
              }
            }, 1000);

            const timeout = setTimeout(() => {
              clearInterval(checkInterval);
              const events = botState.eventLog.slice(eventCount);

              resolve({
                id,
                result: {
                  content: [
                    {
                      type: "text",
                      text: JSON.stringify({
                        success: true,
                        events_detected: events.length,
                        message: `${duration}秒监听结束，未检测到新事件。请确认：\n1. 已在 q.qq.com 后台配置了沙箱环境\n2. 已添加机器人为QQ好友\n3. 已给机器人发送消息`,
                      }),
                    },
                  ],
                },
              });
            }, duration * 1000);
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
  console.error(`[qqbot] MCP server starting...`);
  console.error(`[qqbot] AppID: ${APP_ID ? APP_ID.slice(0, 4) + "..." : "未配置"}`);

  // 后台启动 QQ Bot 连接（不阻塞MCP初始化）
  initBot().then((connected) => {
    if (connected) {
      console.error(`[qqbot] ✅ QQ Bot 连接成功`);
    } else {
      console.error(`[qqbot] ⚠️ QQ Bot 连接中（将在后台重试）`);
    }
  });

  // MCP stdio 主循环
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
        console.error(`[qqbot] parse error: ${err.message}`);
      }
    }
  }

  // 清理
  stopHeartbeat();
  if (botState.ws) {
    try { botState.ws.close(); } catch {}
  }
  console.error(`[qqbot] Server shutting down`);
}

main().catch((err) => {
  console.error(`[qqbot] fatal: ${err.message}`);
  process.exit(1);
});
