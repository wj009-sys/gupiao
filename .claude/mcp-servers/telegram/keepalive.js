#!/usr/bin/env node
/**
 * Telegram 后台保活服务 — 常驻监听手机命令并自动执行
 *
 * 独立运行，保持 Telegram 长轮询，自动响应 /情报员 /分析师 等命令。
 * 不依赖 Claude Code，检测到命令后直接运行 Python 脚本并发回结果。
 *
 * 使用方式：
 *   node .claude/mcp-servers/telegram/keepalive.js
 *
 * 环境变量（从 settings.local.json 自动加载）：
 *   TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, SOCKS_PROXY
 *   PYTHON_PATH — Python 解释器路径
 */

const { spawn } = require("child_process");
const fs = require("fs");
const path = require("path");
const http = require("http");
const https = require("https");
const net = require("net");
const tls = require("tls");
const { URL } = require("url");

// ============ 加载配置 ============

function loadSettings() {
  const searchPaths = [
    path.join(__dirname, "..", "..", "settings.local.json"),
    path.join(__dirname, "..", "..", ".claude", "settings.local.json"),
    path.join(process.cwd(), ".claude", "settings.local.json"),
  ];
  for (const sp of searchPaths) {
    try {
      if (fs.existsSync(sp)) {
        const content = JSON.parse(fs.readFileSync(sp, "utf-8"));
        Object.assign(process.env, content.env || {});
        console.error(`[tg-keepalive] Loaded settings from: ${sp}`);
        return;
      }
    } catch {}
  }
  console.error(`[tg-keepalive] ⚠️ settings.local.json not found`);
}

loadSettings();

const BOT_TOKEN = process.env.TELEGRAM_BOT_TOKEN || "";
const CHAT_ID = process.env.TELEGRAM_CHAT_ID || "";
const SOCKS_PROXY = process.env.SOCKS_PROXY || "";
const PYTHON = process.env.PYTHON_PATH
  ? path.join(process.env.PYTHON_PATH, "python.exe")
  : "python";
const API_HOST = "api.telegram.org";

// 项目根目录
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

console.error(`[tg-keepalive] Project root: ${PROJECT_ROOT}`);

// ============ 消息队列 ============

const QUEUE_DIR = path.join(PROJECT_ROOT, ".telegram_queue");
const PENDING_FILE = path.join(QUEUE_DIR, "pending.json");

/** 存储消息到待处理队列 */
function storeMessage(msg) {
  try {
    if (!fs.existsSync(QUEUE_DIR)) fs.mkdirSync(QUEUE_DIR, { recursive: true });
    let queue = [];
    try { queue = JSON.parse(fs.readFileSync(PENDING_FILE, "utf8")); } catch {}
    // 去重: 同 chat_id + 同文本 + 最近30秒的不重复添加
    const now = Date.now();
    queue = queue.filter((m) => m.status !== "done");
    const isDup = queue.some(
      (m) => m.chat_id === msg.chat_id && m.text === msg.text && now - m.timestamp < 30000
    );
    if (isDup) return false;
    queue.push(msg);
    // 最多保留100条
    if (queue.length > 100) queue = queue.slice(-100);
    fs.writeFileSync(PENDING_FILE, JSON.stringify(queue, null, 2), "utf8");
    console.error(`[tg-keepalive] 📥 Queued message from ${msg.from_name}: ${msg.text.slice(0, 60)}`);
    return true;
  } catch (err) {
    console.error(`[tg-keepalive] ⚠️ storeMessage error: ${err.message}`);
    return false;
  }
}

// ============ 命令路由 ============

const COMMANDS = {
  "/情报员": {
    script: "scripts/agent1-情报采集/fetch_all.py",
    agent: "情报员",
    emoji: "📊",
    ack: "📊 正在执行情报采集...",
  },
  "/分析师": {
    script: "scripts/agent2-技术分析/analyze.py",
    agent: "分析师",
    emoji: "📈",
    ack: "📈 正在执行技术分析...",
  },
  "/风控官": {
    script: "scripts/agent3-风控/risk_check.py",
    agent: "风控官",
    emoji: "🛡️",
    ack: "🛡️ 正在执行风控检查...",
  },
  "/复盘师": {
    script: "scripts/agent4-复盘/review.py",
    agent: "复盘师",
    emoji: "🔄",
    ack: "🔄 正在执行复盘分析...",
  },
  "/help": {
    agent: "帮助",
    emoji: "❓",
    ack: null,
    help: true,
  },
  "/start": {
    agent: "帮助",
    emoji: "👋",
    ack: null,
    help: true,
  },
};

// ============ SOCKS5 代理支持 ============

function readExact(socket, n) {
  return new Promise((resolve, reject) => {
    const buf = []; let len = 0;
    const onData = (chunk) => { buf.push(chunk); len += chunk.length; if (len >= n) { socket.removeListener("data", onData); resolve(Buffer.concat(buf)); } };
    socket.on("data", onData);
    socket.on("error", reject);
  });
}

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
        socket.write(Buffer.from([0x05, 0x01, 0x00]));
        const authResp = await readExact(socket, 2);
        if (authResp[0] !== 0x05 || authResp[1] !== 0x00) throw new Error(`SOCKS5 auth fail`);
        const domainBuf = Buffer.from(host, "utf8");
        const portBuf = Buffer.alloc(2); portBuf.writeUInt16BE(port);
        socket.write(Buffer.concat([Buffer.from([0x05, 0x01, 0x00, 0x03, domainBuf.length]), domainBuf, portBuf]));
        const header = await readExact(socket, 4);
        if (header[0] !== 0x05 || header[1] !== 0x00) throw new Error(`SOCKS5 conn fail`);
        let addrLen = header[3] === 1 ? 4 : header[3] === 3 ? (await readExact(socket, 1))[0] : header[3] === 4 ? 16 : (() => { throw new Error("ATYP"); })();
        await readExact(socket, addrLen + 2);
        const tlsSocket = tls.connect({ socket, servername: host, host, port });
        tlsSocket.on("ready", () => resolve(tlsSocket));
        tlsSocket.on("error", reject);
      } catch (e) { socket.destroy(); reject(e); }
    });
    socket.on("error", reject);
  });
}

function tgRequest(method, body) {
  return new Promise((resolve, reject) => {
    const doRequest = (tlsSocket) => {
      const data = body ? JSON.stringify(body) : undefined;
      const opts = {
        hostname: API_HOST,
        path: `/bot${BOT_TOKEN}/${method}`,
        method: body ? "POST" : "GET",
        headers: data ? { "Content-Type": "application/json", "Content-Length": Buffer.byteLength(data) } : {},
        timeout: 30000,
      };
      const req = https.request(tlsSocket ? { ...opts, createConnection: () => tlsSocket, agent: false } : opts, (res) => {
        let chunks = "";
        res.on("data", (c) => (chunks += c));
        res.on("end", () => {
          try { const p = JSON.parse(chunks); if (p.ok) resolve(p); else reject(new Error(p.description)); }
          catch { reject(new Error(chunks.slice(0, 200))); }
        });
      });
      req.on("error", reject);
      req.on("timeout", () => { req.destroy(); reject(new Error("Timeout")); });
      if (data) req.write(data);
      req.end();
    };

    if (SOCKS_PROXY) {
      socks5Connect(API_HOST, 443).then(doRequest).catch(reject);
    } else {
      doRequest();
    }
  });
}

function tgSendMessage(text, chatId) {
  return tgRequest("sendMessage", {
    chat_id: chatId || CHAT_ID,
    text,
    parse_mode: "Markdown",
    disable_web_page_preview: false,
  });
}

// ============ 长轮询监听 ============

let offset = 0;
const offsetFile = path.join(__dirname, ".telegram_offset");
try { offset = parseInt(fs.readFileSync(offsetFile, "utf8").trim(), 10) || 0; } catch { offset = 0; }
let lastCommandTime = 0;

/** 单次轮询 */
async function pollOnce() {
  try {
    const result = await tgRequest(`getUpdates?offset=${offset + 1}&timeout=30&allowed_updates=["message"]`);
    const updates = result.result || [];
    for (const u of updates) {
      if (u.update_id > offset) offset = u.update_id;
      const msg = u.message;
      if (!msg || !msg.text) continue;
      const chatId = msg.chat?.id;
      const text = msg.text.trim();
      const isGroup = msg.chat?.type === "group" || msg.chat?.type === "supergroup";

      // 群聊中只响应 @机器人 的消息
      if (isGroup && !text.includes("@")) continue;

      // 解析命令
      let cmd = text.split(/\s+/)[0];
      if (isGroup && cmd.includes("@")) cmd = cmd.split("@")[0];
      cmd = cmd.toLowerCase();

      // 匹配命令
      const matchedKey = Object.keys(COMMANDS).find((k) => cmd === k || cmd === k.toLowerCase());
      if (matchedKey) {
        await handleCommand(matchedKey, chatId || CHAT_ID, text);
      } else if (chatId && text) {
        // 非命令消息 → 写入队列，等待 Claude Code 处理
        const srcMsg = msg; // 保留原始 Telegram 消息对象引用
        const queueEntry = {
          id: `${Date.now()}-${chatId}`,
          text: text,
          chat_id: chatId,
          from_name: srcMsg.from?.first_name || srcMsg.from?.username || "User",
          timestamp: Date.now(),
          status: "pending",
        };
        const stored = storeMessage(queueEntry);
        if (stored) {
          try {
            await tgSendMessage(
              "📨 *消息已收到！*\n\n我正在处理你的消息，请稍候片刻...\n\n" +
              "> *提示：* 实时回复需要 Claude Code 正在运行。\n" +
              "> 如果长时间未收到回复，请稍后重试。",
              chatId
            );
          } catch {}
        }
      }
    }
    try { fs.writeFileSync(offsetFile, String(offset), "utf8"); } catch {}
  } catch (err) {
    // 静默处理轮询错误（网络波动）
  }
}

/** 执行命令 */
async function handleCommand(cmdKey, chatId, rawText) {
  const cmd = COMMANDS[cmdKey];
  if (!cmd) return;

  console.error(`[tg-keepalive] 🎯 Command: ${rawText} from chat ${chatId}`);

  // 帮助命令
  if (cmd.help) {
    const helpText =
      "👋 *股票投研机器人*\n\n" +
      "可用命令：\n" +
      "📊 `/情报员` — 情报采集\n" +
      "📈 `/分析师` — 技术分析\n" +
      "🛡️ `/风控官` — 风控检查\n" +
      "🔄 `/复盘师` — 复盘分析\n\n" +
      "命令在 Claude Code 中运行，结果会自动推送到这里。";
    try { await tgSendMessage(helpText, chatId); } catch {}
    return;
  }

  // 发送确认
  try { await tgSendMessage(cmd.ack, chatId); } catch {}

  // 防止短时间内重复执行
  const now = Date.now();
  if (now - lastCommandTime < 10000) {
    try { await tgSendMessage("⏳ 上一个任务正在执行中，请稍候...", chatId); } catch {}
    return;
  }
  lastCommandTime = now;

  // 运行 Python 脚本
  const scriptPath = path.join(PROJECT_ROOT, cmd.script);
  if (!fs.existsSync(scriptPath)) {
    try { await tgSendMessage(`❌ 脚本不存在: ${cmd.script}`, chatId); } catch {}
    return;
  }

  console.error(`[tg-keepalive] ▶ Running: ${cmd.script}`);

  try {
    const result = await runPythonScript(scriptPath, chatId);
    if (result.success) {
      const summary = `✅ *${cmd.emoji} ${cmd.agent} 执行完成*\n\n${result.summary || "报告已生成"}`;
      await tgSendMessage(summary, chatId);

      // 如果有报告文件，发送链接
      if (result.reportFile) {
        const reportMsg = `📄 完整报告：\`${result.reportFile}\``;
        await tgSendMessage(reportMsg, chatId);
      }
    } else {
      await tgSendMessage(`❌ *${cmd.agent} 执行失败*\n\n${result.error}`, chatId);
    }
  } catch (err) {
    console.error(`[tg-keepalive] ❌ Error: ${err.message}`);
    try { await tgSendMessage(`❌ 执行异常: ${err.message}`, chatId); } catch {}
  }
}

/** 运行 Python 脚本 */
function runPythonScript(scriptPath, chatId) {
  return new Promise((resolve) => {
    const isWin = process.platform === "win32";
    const args = ["-X", "utf8", scriptPath];

    // 获取今天的日期作为参数
    const today = new Date();
    const dateStr = `${today.getFullYear()}${String(today.getMonth() + 1).padStart(2, "0")}${String(today.getDate()).padStart(2, "0")}`;
    args.push(dateStr);

    const proc = spawn(PYTHON, args, {
      cwd: PROJECT_ROOT,
      shell: isWin,
      stdio: ["ignore", "pipe", "pipe"],
      env: { ...process.env },
    });

    let stdout = "";
    let stderr = "";

    proc.stdout.on("data", (d) => { stdout += d.toString(); });
    proc.stderr.on("data", (d) => { stderr += d.toString(); });

    // 超时 120 秒
    const timeout = setTimeout(() => {
      proc.kill();
      resolve({ success: false, error: "执行超时（120秒）" });
    }, 120000);

    proc.on("close", (code) => {
      clearTimeout(timeout);

      // 尝试从 stdout/stderr 提取摘要或报告路径
      const allOutput = stdout + stderr;

      // 提取报告文件路径
      const reportMatch = allOutput.match(/reports?[\/\\][^\s"'`]+\.(md|json)/i);
      const reportFile = reportMatch ? reportMatch[0] : null;

      // 提取有用的摘要（最后几行非错误信息）
      const lines = stdout.split("\n").filter((l) => l.trim() && !l.includes("Error") && !l.includes("Traceback"));
      const summary = lines.slice(-3).join("\n") || null;

      if (code === 0) {
        resolve({ success: true, summary, reportFile });
      } else {
        const error = stderr.split("\n").filter((l) => l.trim()).slice(-3).join("\n") || `退出码: ${code}`;
        resolve({ success: false, error });
      }
    });

    proc.on("error", (err) => {
      clearTimeout(timeout);
      resolve({ success: false, error: `启动失败: ${err.message}` });
    });
  });
}

// ============ HTTP API（供 MCP 查询状态） ============

function startHttpServer(port = 19786) {
  const server = http.createServer((req, res) => {
    res.setHeader("Access-Control-Allow-Origin", "*");
    res.setHeader("Access-Control-Allow-Methods", "GET, POST, OPTIONS");
    res.setHeader("Access-Control-Allow-Headers", "Content-Type");

    if (req.method === "OPTIONS") { res.writeHead(204); res.end(); return; }

    if (req.url === "/status" && req.method === "GET") {
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(JSON.stringify({
        status: "running",
        bot: BOT_TOKEN ? `${BOT_TOKEN.slice(0, 8)}...` : "unconfigured",
        chat_id: CHAT_ID || "unconfigured",
        proxy: SOCKS_PROXY ? "configured" : "none",
        uptime: Math.floor((Date.now() - startTime) / 1000),
        last_command: lastCommandTime ? new Date(lastCommandTime).toISOString() : null,
      }));
      return;
    }

    res.writeHead(404);
    res.end(JSON.stringify({ error: "Not found" }));
  });

  server.listen(port, "127.0.0.1", () => {
    console.error(`[tg-keepalive] 🌐 HTTP API on http://127.0.0.1:${port}/status`);
  });
  server.on("error", (err) => {
    console.error(`[tg-keepalive] ⚠️ HTTP server error: ${err.message}`);
  });
}

// ============ 主循环 ============

const startTime = Date.now();

async function main() {
  console.error("");
  console.error(`╔══════════════════════════════════════════╗`);
  console.error(`║    Telegram 常驻监听服务                  ║`);
  console.error(`║    自动响应手机命令                        ║`);
  console.error(`╚══════════════════════════════════════════╝`);
  console.error(`[tg-keepalive] Bot: @${BOT_TOKEN ? "..." + BOT_TOKEN.slice(-6) : "未配置"}`);
  console.error(`[tg-keepalive] Python: ${PYTHON}`);
  console.error(`[tg-keepalive] Proxy: ${SOCKS_PROXY || "none"}`);

  if (!BOT_TOKEN || !CHAT_ID) {
    console.error(`[tg-keepalive] ❌ TELEGRAM_BOT_TOKEN 或 TELEGRAM_CHAT_ID 未配置`);
    console.error(`[tg-keepalive] 请在 settings.local.json 中设置这两个值`);
    process.exit(1);
  }

  // 验证 Bot Token
  try {
    const me = await tgRequest("getMe");
    console.error(`[tg-keepalive] ✅ Bot: @${me.result?.username || "?"}`);
  } catch (err) {
    console.error(`[tg-keepalive] ❌ Bot 验证失败: ${err.message}`);
    console.error(`[tg-keepalive] 请检查 TELEGRAM_BOT_TOKEN 和代理配置`);
    process.exit(1);
  }

  startHttpServer();

  console.error(`[tg-keepalive] 📡 开始监听命令...`);
  console.error(`[tg-keepalive] 在 Telegram 中给机器人发 /情报员、/分析师 等命令`);

  // 无限轮询
  let failCount = 0;
  while (true) {
    try {
      await pollOnce();
      failCount = 0;
    } catch (err) {
      failCount++;
      console.error(`[tg-keepalive] ⚠️ Poll error (${failCount}): ${err.message}`);
      // 连续失败时逐步增加延迟
      await new Promise((r) => setTimeout(r, Math.min(failCount * 1000, 30000)));
    }
  }
}

main().catch((err) => {
  console.error(`[tg-keepalive] ❌ Fatal: ${err.message}`);
  process.exit(1);
});
