#!/usr/bin/env node
/**
 * QQ Bot 后台保活服务 — 常驻监听手机命令并自动执行
 *
 * 独立运行，保持 QQ Bot WebSocket 持久在线。
 * 自动响应 /情报员 /分析师 等命令，运行对应 Python 脚本并回传结果。
 *
 * 使用方式：
 *   node .claude/mcp-servers/qqbot/keepalive.js
 *
 * 环境变量（从 settings.local.json 加载）：
 *   QQBOT_APP_ID, QQBOT_APP_SECRET
 *   PYTHON_PATH — Python 解释器路径
 *
 * 命令行参数：
 *   --settings <path>  指定 settings.local.json 路径（默认自动查找）
 */

const https = require("https");
const http = require("http");
const fs = require("fs");
const path = require("path");
const { spawn } = require("child_process");

// cc-bridge：Claude Code AI 对话引擎
const { ClaudeCodeSession } = require(path.join(
  __dirname, "..", "..", "..", "cc-bridge", "index.js"
));

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
  pendingSends: [],
  httpServer: null,
  lastCommandTime: 0,
};

// ============ 项目配置 ============

const PYTHON = process.env.PYTHON_PATH
  ? path.join(process.env.PYTHON_PATH, "python.exe")
  : "python";

const PROJECT_ROOT = (() => {
  for (const p of [__dirname, process.cwd()].flatMap((d) => [
    d,
    path.resolve(d, ".."),
    path.resolve(d, "..", ".."),
  ])) {
    if (fs.existsSync(path.join(p, "CLAUDE.md"))) return p;
  }
  return process.cwd();
})();

// ============ 命令路由 ============

const COMMANDS = {
  "/情报员": { script: "scripts/agent1-情报采集/fetch_all.py", agent: "情报员", emoji: "📊", ack: "📊 正在执行情报采集..." },
  "/分析师": { script: "scripts/agent2-技术分析/analyze.py", agent: "分析师", emoji: "📈", ack: "📈 正在执行技术分析..." },
  "/风控官": { script: "scripts/agent3-风控/risk_check.py", agent: "风控官", emoji: "🛡️", ack: "🛡️ 正在执行风控检查..." },
  "/复盘师": { script: "scripts/agent4-复盘/review.py", agent: "复盘师", emoji: "🔄", ack: "🔄 正在执行复盘分析..." },
  "/选股": { script: "scripts/agent5-选股/stock_picker.py", agent: "选股机器人", emoji: "🔍", ack: "🔍 正在执行多因子选股..." },
  "/选股机器人": { script: "scripts/agent5-选股/stock_picker.py", agent: "选股机器人", emoji: "🔍", ack: "🔍 正在执行多因子选股..." },
  "/操盘": { script: "scripts/agent6-操盘/trader.py", agent: "操盘手", emoji: "🎯", ack: "🎯 正在制定交易计划..." },
  "/操盘手": { script: "scripts/agent6-操盘/trader.py", agent: "操盘手", emoji: "🎯", ack: "🎯 正在制定交易计划..." },
  "/决策": { script: "scripts/agent7-决策/leader.py", agent: "投资领导", emoji: "🏆", ack: "🏆 正在综合决策..." },
  "/投资领导": { script: "scripts/agent7-决策/leader.py", agent: "投资领导", emoji: "🏆", ack: "🏆 正在综合决策..." },
  "/help": { agent: "帮助", emoji: "❓", help: true },
  "/start": { agent: "帮助", emoji: "👋", help: true },
};

const AGENT_NAMES = { 1: "情报员", 2: "分析师", 3: "风控官", 4: "复盘师", 5: "选股机器人", 6: "操盘手", 7: "投资领导" };

/** 通过 QQ API 发送消息 */
async function qqSendMessage(openid, content, isGroup = false) {
  try {
    const endpoint = isGroup
      ? `/v2/groups/${openid}/messages`
      : `/v2/users/${openid}/messages`;
    const bodyData = { content: JSON.stringify([{ type: 1, content }]), msg_type: 0 };
    if (!isGroup) bodyData.openid = openid;

    await ensureAccessToken();
    await httpRequest({
      hostname: "api.sgroup.qq.com",
      path: endpoint,
      method: "POST",
      headers: {
        Authorization: `QQBot ${state.accessToken}`,
        "Content-Type": "application/json",
        "X-Union-Appid": APP_ID,
      },
    }, bodyData);
  } catch (err) {
    console.error(`[qqbot-keepalive] ⚠️ Send failed: ${err.message}`);
  }
}

async function ensureAccessToken() {
  if (!state.accessToken || Date.now() >= state.tokenExpiresAt - 300000) {
    await getAccessToken();
  }
}

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

// ============ 命令执行 ============

/** 运行 Python 脚本并返回结果 */
function runPythonScript(scriptRelPath) {
  return new Promise((resolve) => {
    const scriptPath = path.join(PROJECT_ROOT, scriptRelPath);
    if (!fs.existsSync(scriptPath)) {
      return resolve({ success: false, error: `脚本不存在: ${scriptRelPath}` });
    }

    const isWin = process.platform === "win32";
    const today = new Date();
    const dateStr = `${today.getFullYear()}${String(today.getMonth() + 1).padStart(2, "0")}${String(today.getDate()).padStart(2, "0")}`;

    const proc = spawn(PYTHON, ["-X", "utf8", scriptPath, dateStr], {
      cwd: PROJECT_ROOT,
      shell: isWin,
      stdio: ["ignore", "pipe", "pipe"],
      env: { ...process.env },
    });

    let stdout = "", stderr = "";
    proc.stdout.on("data", (d) => { stdout += d.toString(); });
    proc.stderr.on("data", (d) => { stderr += d.toString(); });

    const timeout = setTimeout(() => { proc.kill(); resolve({ success: false, error: "执行超时（120秒）" }); }, 120000);

    proc.on("close", (code) => {
      clearTimeout(timeout);
      const allOutput = stdout + stderr;
      const reportMatch = allOutput.match(/reports?[\/\\][^\s"'`]+\.(md|json)/i);
      const lines = stdout.split("\n").filter((l) => l.trim() && !l.includes("Error") && !l.includes("Traceback"));
      const summary = lines.slice(-3).join("\n") || null;
      if (code === 0) resolve({ success: true, summary, reportFile: reportMatch ? reportMatch[0] : null });
      else {
        const error = stderr.split("\n").filter((l) => l.trim()).slice(-3).join("\n") || `退出码: ${code}`;
        resolve({ success: false, error });
      }
    });
    proc.on("error", (err) => { clearTimeout(timeout); resolve({ success: false, error: `启动失败: ${err.message}` }); });
  });
}

/** 处理 QQ Bot 收到的命令 */
async function handleQQCommand(cmdKey, openid, isGroup, rawText) {
  const cmd = COMMANDS[cmdKey];
  if (!cmd) return;

  console.error(`[qqbot-keepalive] 🎯 Command: ${rawText} from ${openid}`);

  if (cmd.help) {
    const helpText =
      "👋 股票投研机器人\n\n可用命令：\n📊 /情报员\n📈 /分析师\n🛡️ /风控官\n🔄 /复盘师\n\n命令在后台运行，结果会自动推送。";
    await qqSendMessage(openid, helpText, isGroup);
    return;
  }

  // 发送确认
  await qqSendMessage(openid, cmd.ack, isGroup);

  // 防重复
  const now = Date.now();
  if (now - state.lastCommandTime < 10000) {
    await qqSendMessage(openid, "⏳ 上一个任务正在执行中，请稍候...", isGroup);
    return;
  }
  state.lastCommandTime = now;

  const result = await runPythonScript(cmd.script);
  if (result.success) {
    const reply = `✅ ${cmd.emoji} ${cmd.agent} 执行完成\n\n${result.summary || "报告已生成"}`;
    await qqSendMessage(openid, reply, isGroup);
    if (result.reportFile) {
      await qqSendMessage(openid, `📄 ${result.reportFile}`, isGroup);
    }
  } else {
    await qqSendMessage(openid, `❌ ${cmd.agent} 执行失败\n\n${result.error}`, isGroup);
  }
}

/** 解析消息中的命令 */
function parseCommand(text) {
  if (!text) return null;
  let cmd = text.trim().split(/\s+/)[0];
  return Object.keys(COMMANDS).find((k) => cmd === k || cmd === k.toLowerCase()) || null;
}

// ============ cc-bridge AI 对话引擎 ============

let ccSession = null;
let ccProcessing = false;

function initCCSession() {
  if (ccSession) return;
  ccSession = new ClaudeCodeSession({
    projectRoot: PROJECT_ROOT,
    timeout: 180000,
    skipPermissions: true,
    bare: true,
    appendSystemPrompt: [
      "你正在和用户通过聊天平台（QQ）对话，所有回复都会推送到用户手机。",
      "请用中文回复，保持简洁（建议不超过200字）。",
      "关于股票投研项目：",
      "- 用户可能问 A 股相关问题，结合你的金融知识给出分析",
      "- 当需要最新数据时，利用你的知识回答，不要使用 Bash 等工具",
      "- 回答中不要提及你是 Claude Code",
    ].join("\n"),
  });
  console.error("[qqbot-keepalive] 🧠 cc-bridge session created");
}

/** 通过 cc-bridge 用 Claude Code AI 处理 QQ 消息 */
async function handleAIDialog(text, openid, isGroup, fromName) {
  console.error(`[qqbot-keepalive] 🧠 AI dialog from ${fromName}: ${text.slice(0, 80)}`);
  await qqSendMessage(openid, "🧠 正在思考，请稍候...", isGroup);

  initCCSession();
  if (!ccSession) {
    await qqSendMessage(openid, "❌ AI 引擎未就绪", isGroup);
    return;
  }

  try {
    await ccSession.start();
    const result = await ccSession.send(text);
    if (result && result.text) {
      const reply = result.text.length > 4000 ? result.text.slice(0, 3997) + "..." : result.text;
      await qqSendMessage(openid, reply, isGroup);
      console.error(`[qqbot-keepalive] ✅ AI reply (${result.text.length} chars, $${(result.cost || 0).toFixed(4)})`);
    }
  } catch (err) {
    console.error(`[qqbot-keepalive] ❌ AI dialog error: ${err.message}`);
    if (err.message.includes("session") || err.message.includes("resume")) {
      try { ccSession.stop(); } catch {}
      ccSession = null;
    }
    await qqSendMessage(openid, `❌ 处理出错: ${err.message.slice(0, 100)}`, isGroup);
  }
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
              } else if (t === "C2C_MESSAGE_CREATE") {
                const openid = d.author?.user_openid;
                const content = d.content || "";
                const cmd = parseCommand(content);
                if (cmd && openid) {
                  handleQQCommand(cmd, openid, false, content);
                } else if (openid && content && !ccProcessing) {
                  ccProcessing = true;
                  handleAIDialog(content, openid, false, d.author?.member_openid || "User").finally(() => { ccProcessing = false; });
                }
              } else if (t === "AT_MESSAGE_CREATE") {
                const openid = d.group_openid;
                const content = (d.content || "").replace(/<@!\d+>/g, "").trim();
                const cmd = parseCommand(content);
                if (cmd && openid) {
                  handleQQCommand(cmd, openid, true, content);
                } else if (openid && content && !ccProcessing) {
                  ccProcessing = true;
                  handleAIDialog(content, openid, true, d.author?.member_openid || "User").finally(() => { ccProcessing = false; });
                }
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

  // 初始化 cc-bridge（后台预启动）
  initCCSession();
  ccSession.start().then(() => {
    console.error(`[qqbot-keepalive] 🧠 cc-bridge ready (session: ${ccSession.getStatus().session_id || "new"})`);
  }).catch((err) => {
    console.error(`[qqbot-keepalive] ⚠️ cc-bridge init: ${err.message}`);
  });

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
