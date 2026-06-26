#!/usr/bin/env node
/**
 * QQ Bot 后台保活服务
 *
 * 独立运行，保持 QQ Bot WebSocket 持久在线。
 * 可在 Windows 后台长期运行，供 MCP 服务器调用发送消息。
 *
 * 使用方式：
 *   node .claude/mcp-servers/qqbot/keepalive.js
 *
 * 环境变量（从 settings.local.json 加载）：
 *   QQBOT_APP_ID, QQBOT_APP_SECRET
 *
 * 命令行参数：
 *   --settings <path>  指定 settings.local.json 路径（默认自动查找）
 */

const https = require("https");
const http = require("http");
const fs = require("fs");
const path = require("path");

// ============ 加载配置 ============

function loadSettings() {
  // 从项目根目录查找 settings.local.json
  const searchPaths = [
    path.join(__dirname, "..", "..", "settings.local.json"),
    path.join(__dirname, "..", "..", ".claude", "settings.local.json"),
    process.cwd() + "/.claude/settings.local.json",
  ];

  for (const sp of searchPaths) {
    try {
      if (fs.existsSync(sp)) {
        const content = JSON.parse(fs.readFileSync(sp, "utf-8"));
        const env = content.env || {};
        Object.assign(process.env, env);
        console.error(`[qqbot-keepalive] Loaded settings from: ${sp}`);
        return;
      }
    } catch {}
  }

  console.error(`[qqbot-keepalive] ⚠️  Could not find settings.local.json, using existing env vars`);
}

loadSettings();

const APP_ID = process.env.QQBOT_APP_ID || "";
const APP_SECRET = process.env.QQBOT_APP_SECRET || "";

// ============ QQ Bot 状态 ============

let state = {
  accessToken: "",
  tokenExpiresAt: 0,
  ws: null,
  ready: false,
  sessionId: "",
  lastSeq: null,
  heartbeatInterval: null,
  reconnectAttempts: 0,
  maxReconnectAttempts: 50,
  // 消息发送队列
  pendingSends: [],
  httpServer: null,
};

// ============ HTTP API ============

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
            resolve(JSON.parse(data));
          } catch {
            reject(new Error(`Parse error: ${data.slice(0, 200)}`));
          }
        });
      }
    );
    req.on("error", reject);
    req.on("timeout", () => { req.destroy(); reject(new Error("Timeout")); });
    if (body) req.write(typeof body === "string" ? body : JSON.stringify(body));
    req.end();
  });
}

// ============ QQ Bot API ============

async function getAccessToken() {
  const result = await httpRequest(
    {
      hostname: "bots.qq.com",
      path: "/app/getAppAccessToken",
      method: "POST",
      headers: { "Content-Type": "application/json" },
    },
    { appId: APP_ID, clientSecret: APP_SECRET }
  );
  if (result.access_token) {
    state.accessToken = result.access_token;
    state.tokenExpiresAt = Date.now() + (result.expires_in || 7200) * 1000;
    console.error(`[qqbot-keepalive] ✅ Token acquired, expires in ${result.expires_in || 7200}s`);
    return result.access_token;
  }
  throw new Error(`Token failed: ${JSON.stringify(result)}`);
}

async function getGateway() {
  const result = await httpRequest({
    hostname: "api.sgroup.qq.com",
    path: "/gateway/bot",
    headers: { Authorization: `QQBot ${state.accessToken}` },
  });
  if (result.url) return result.url;
  throw new Error(`Gateway failed: ${JSON.stringify(result)}`);
}

// ============ WebSocket ============

function startHeartbeat(interval) {
  if (state.heartbeatInterval) clearInterval(state.heartbeatInterval);
  state.heartbeatInterval = setInterval(() => {
    if (state.ws && state.ws.readyState === WebSocket.OPEN) {
      state.ws.send(JSON.stringify({ op: 1, d: state.lastSeq }));
    }
  }, interval);
  console.error(`[qqbot-keepalive] 💓 Heartbeat started (${interval}ms)`);
}

function identify() {
  if (!state.ws || state.ws.readyState !== WebSocket.OPEN) return;
  const payload = state.sessionId
    ? { op: 6, d: { token: `QQBot ${state.accessToken}`, session_id: state.sessionId, seq: state.lastSeq } }
    : { op: 2, d: { token: `QQBot ${state.accessToken}`, intents: 1 << 25 | 1 << 30, shard: [0, 1] } };
  state.ws.send(JSON.stringify(payload));
  console.error(`[qqbot-keepalive] Sent ${state.sessionId ? 'Resume' : 'Identify'}`);
}

async function connect() {
  if (!APP_ID || !APP_SECRET) {
    console.error(`[qqbot-keepalive] ❌ AppID/AppSecret 未配置`);
    return false;
  }

  try {
    if (!state.accessToken || Date.now() >= state.tokenExpiresAt - 300000) {
      await getAccessToken();
    }

    const gatewayUrl = await getGateway();
    const wsUrl = `${gatewayUrl}?v=1&encoding=json`;

    return new Promise((resolve) => {
      const ws = new WebSocket(wsUrl);
      state.ws = ws;

      ws.onopen = () => console.error(`[qqbot-keepalive] 🔗 WebSocket connected`);

      ws.onmessage = (event) => {
        try {
          const { op, d, s, t } = JSON.parse(event.data);
          switch (op) {
            case 0:
              state.lastSeq = s;
              if (t === "READY") {
                state.sessionId = d.session_id;
                state.ready = true;
                state.reconnectAttempts = 0;
                console.error(`[qqbot-keepalive] ✅ Ready! Session: ${d.session_id}`);
                console.error(`[qqbot-keepalive] 🤖 Bot: ${d.user?.username || "?"} (id=${d.user?.id || "?"})`);
                resolve(true);
              } else if (t === "RESUMED") {
                state.ready = true;
                console.error(`[qqbot-keepalive] ✅ Session resumed`);
              }
              break;
            case 7:
              console.error(`[qqbot-keepalive] 🔄 Reconnect requested, reconnecting...`);
              ws.close();
              break;
            case 9:
              console.error(`[qqbot-keepalive] ⚠️ Invalid session, re-identifying...`);
              state.sessionId = "";
              identify();
              break;
            case 10:
              startHeartbeat(d.heartbeat_interval);
              identify();
              break;
            case 11:
              break; // Heartbeat ACK, silent
          }
        } catch {}
      };

      ws.onclose = () => {
        state.ready = false;
        console.error(`[qqbot-keepalive] 🔌 WebSocket closed`);
        scheduleReconnect();
        resolve(false);
      };

      ws.onerror = () => {
        console.error(`[qqbot-keepalive] ❌ WebSocket error`);
        resolve(false);
      };

      // 超时
      setTimeout(() => resolve(false), 20000);
    });
  } catch (err) {
    console.error(`[qqbot-keepalive] ❌ Connection error: ${err.message}`);
    return false;
  }
}

function scheduleReconnect() {
  if (state.reconnectAttempts >= state.maxReconnectAttempts) {
    console.error(`[qqbot-keepalive] ❌ Max reconnects reached`);
    return;
  }
  const delay = Math.min(1000 * Math.pow(2, state.reconnectAttempts), 30000);
  state.reconnectAttempts++;
  console.error(`[qqbot-keepalive] 📅 Reconnecting in ${delay}ms (attempt ${state.reconnectAttempts})`);
  setTimeout(async () => {
    const ok = await connect();
    if (!ok && state.reconnectAttempts < state.maxReconnectAttempts) {
      scheduleReconnect();
    }
  }, delay);
}

// ============ 本地 HTTP 服务器（供 MCP 调用） ============

function startHttpServer(port = 19785) {
  const server = http.createServer((req, res) => {
    // CORS
    res.setHeader("Access-Control-Allow-Origin", "*");
    res.setHeader("Access-Control-Allow-Methods", "POST, GET, OPTIONS");
    res.setHeader("Access-Control-Allow-Headers", "Content-Type");

    if (req.method === "OPTIONS") {
      res.writeHead(204);
      res.end();
      return;
    }

    if (req.method !== "POST") {
      res.writeHead(405);
      res.end(JSON.stringify({ error: "Method not allowed" }));
      return;
    }

    let body = "";
    req.on("data", (c) => (body += c));
    req.on("end", async () => {
      try {
        const parsed = JSON.parse(body);
        const { action, ...params } = parsed;

        if (action === "status") {
          res.writeHead(200);
          res.end(JSON.stringify({
            ready: state.ready,
            sessionId: state.sessionId,
            uptime: process.uptime(),
            reconnectAttempts: state.reconnectAttempts,
          }));
          return;
        }

        if (action === "send") {
          const { content, target_type, msg_type = 0 } = params;
          if (!content) throw new Error("Missing content");

          if (!state.ready) throw new Error("Bot not ready");

          const endpoint = target_type === "group"
            ? `/v2/groups/${params.target_id}/messages`
            : `/v2/users/${params.target_id}/messages`;

          const bodyData = { msg_type };
          if (msg_type === 0) bodyData.content = content;
          else if (msg_type === 2) bodyData.markdown = { content };

          const result = await httpRequest({
            hostname: "api.sgroup.qq.com",
            path: endpoint,
            method: "POST",
            headers: {
              Authorization: `QQBot ${state.accessToken}`,
              "Content-Type": "application/json",
              "X-Union-Appid": APP_ID,
            },
          }, bodyData);

          res.writeHead(200);
          res.end(JSON.stringify({ success: true, data: result }));
          return;
        }

        res.writeHead(400);
        res.end(JSON.stringify({ error: `Unknown action: ${action}` }));
      } catch (err) {
        res.writeHead(500);
        res.end(JSON.stringify({ error: err.message }));
      }
    });
  });

  server.listen(port, "127.0.0.1", () => {
    console.error(`[qqbot-keepalive] 🌐 HTTP server running on http://127.0.0.1:${port}`);
    console.error(`[qqbot-keepalive] 📡 POST / with JSON: { "action": "send", "target_id": "...", "content": "..." }`);
    console.error(`[qqbot-keepalive] 📡 POST / with JSON: { "action": "status" }`);
  });

  state.httpServer = server;
}

// ============ 启动 ============

async function main() {
  console.error(`
╔══════════════════════════════════════════╗
║        QQ Bot 后台保活服务 v1.0          ║
║  保持 QQ 机器人 WebSocket 持久在线        ║
║  提供本地 HTTP API 供 MCP 服务器调用      ║
╚══════════════════════════════════════════╝
  `);

  if (!APP_ID || !APP_SECRET) {
    console.error(`[qqbot-keepalive] ❌ 错误：QQBOT_APP_ID 和 QQBOT_APP_SECRET 未配置`);
    console.error(`  请在 .claude/settings.local.json 中配置：`);
    console.error(`    "QQBOT_APP_ID": "你的机器人AppID"`);
    console.error(`    "QQBOT_APP_SECRET": "你的机器人AppSecret"`);
    process.exit(1);
  }

  // 启动 HTTP 供 MCP 调用
  startHttpServer();

  // 连接 WebSocket
  console.error(`[qqbot-keepalive] 🔌 Connecting to QQ Gateway...`);
  const connected = await connect();

  if (connected) {
    console.error(`[qqbot-keepalive] ✅ QQ Bot 已在线！`);
  } else {
    console.error(`[qqbot-keepalive] ⚠️ 初始连接失败，将在后台重试...`);
    // 主动触发重连（catch 块中的错误不会自动触发 scheduleReconnect）
    state.reconnectAttempts = 0;
    scheduleReconnect();
  }

  // 保持进程运行
  const shutdown = () => {
    console.error(`\n[qqbot-keepalive] Shutting down...`);
    if (state.heartbeatInterval) clearInterval(state.heartbeatInterval);
    if (state.ws) try { state.ws.close(); } catch {}
    if (state.httpServer) state.httpServer.close();
    process.exit(0);
  };

  process.on("SIGINT", shutdown);
  process.on("SIGTERM", shutdown);

  // 定期检查 token 是否过期，提前刷新
  setInterval(async () => {
    if (Date.now() >= state.tokenExpiresAt - 600000) {
      try {
        console.error(`[qqbot-keepalive] 🔄 Token expiring, refreshing...`);
        await getAccessToken();
      } catch (err) {
        console.error(`[qqbot-keepalive] Token refresh failed: ${err.message}`);
      }
    }
  }, 300000);

  console.error(`[qqbot-keepalive] ✅ 服务运行中...`);
}

main().catch((err) => {
  console.error(`[qqbot-keepalive] Fatal: ${err.message}`);
  process.exit(1);
});
