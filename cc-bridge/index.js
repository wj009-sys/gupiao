#!/usr/bin/env node
/**
 * cc-bridge — Claude Code 桥接模块
 *
 * 借鉴 cc-connect (github.com/chenhg5/cc-connect) 的核心架构：
 * 每条消息通过 --print 模式启动独立 Claude Code 进程，
 * 使用 --session-id 保持对话上下文连续性。
 *
 * 输出格式：stream-json，无需解析终端控制字符。
 *
 * 用法:
 *   const { ClaudeCodeSession } = require('./cc-bridge');
 *   const session = new ClaudeCodeSession({ projectRoot: __dirname });
 *   await session.start();
 *   const reply = await session.send("你好");
 *   await session.stop();
 */

const { spawn } = require("child_process");
const { EventEmitter } = require("events");
const path = require("path");
const fs = require("fs");

class ClaudeCodeSession extends EventEmitter {
  /**
   * @param {Object} options
   * @param {string} [options.projectRoot] - 项目根目录
   * @param {number} [options.timeout=120000] - 单次回复超时
   * @param {string} [options.appendSystemPrompt] - 附加系统提示
   * @param {boolean} [options.skipPermissions=true] - 自动跳过权限确认
   * @param {boolean} [options.bare=true] - 最小模式（跳过插件/LSP等）
   * @param {string} [options.sessionId] - 指定 session ID
   * @param {string} [modelsDir] - 持久化目录
   */
  constructor(options = {}) {
    super();
    this.options = {
      projectRoot: options.projectRoot || process.cwd(),
      timeout: options.timeout || 120000,
      skipPermissions: options.skipPermissions !== false,
      bare: options.bare !== false,
      sessionId: options.sessionId || null,
      appendSystemPrompt:
        options.appendSystemPrompt ||
        [
          "你正在和用户通过聊天平台对话，所有回复都会推送到用户手机。",
          "请用中文回复，保持简洁。",
          "",
          "关于股票投研项目：",
          "- 需要实时数据时，请使用你的内置知识回答",
          "- 用户可能问 A 股相关问题，结合你的金融知识给出分析",
          '- 回答中不要提及你是 Claude Code，只说「我」',
          "- 不要使用工具（Bash、Edit等），仅用文本回复",
        ].join("\n"),
    };

    this._sessionId = this.options.sessionId;
    this._claudeBinary = this._findClaudeBinary();
    this._dataDir = path.join(this.options.projectRoot, ".cc-bridge");
    this._sessionFile = path.join(this._dataDir, "session_id");

    // 加载持久化的 session ID
    try {
      if (fs.existsSync(this._sessionFile)) {
        this._sessionId = fs.readFileSync(this._sessionFile, "utf8").trim();
      }
    } catch {}

    this._running = false;
  }

  /** 查找 claude.exe 路径 */
  _findClaudeBinary() {
    const npmDir = process.env.APPDATA
      ? path.join(process.env.APPDATA, "npm")
      : "";
    const candidates = [
      path.join(npmDir, "node_modules", "@anthropic-ai", "claude-code", "bin", "claude.exe"),
      path.join(npmDir, "claude.cmd"),
      path.join(npmDir, "claude"),
    ];
    for (const c of candidates) {
      try { if (fs.existsSync(c)) return c; } catch {}
    }
    return "claude";
  }

  /** 构建 CLI 参数 */
  _buildArgs(extraPrompt) {
    const args = [
      "--print",
      "--output-format", "stream-json",
      "--verbose",
    ];

    // 会话延续：首次用 --append-system-prompt，后续用 --resume
    if (this._sessionId) {
      // 已有 session → 用 --resume 续传
      args.push("--resume", this._sessionId);
    }

    if (this.options.skipPermissions) {
      args.push("--dangerously-skip-permissions");
    }

    if (this.options.bare) {
      args.push("--bare");
    }

    // --append-system-prompt 只在首次有效，resume 时不需要
    if (!this._sessionId && this.options.appendSystemPrompt) {
      args.push("--append-system-prompt", this.options.appendSystemPrompt);
    }

    if (extraPrompt) {
      args.push(extraPrompt);
    }

    return args;
  }

  /**
   * 启动 — 准备目录，不生成 session ID（首次 send 时创建）
   */
  async start() {
    if (this._running) return;
    this._running = true;

    // 确保数据目录存在
    try {
      if (!fs.existsSync(this._dataDir)) {
        fs.mkdirSync(this._dataDir, { recursive: true });
      }
    } catch {}

    // 从文件加载已有的 session ID（resume 用）
    if (!this._sessionId) {
      try {
        if (fs.existsSync(this._sessionFile)) {
          this._sessionId = fs.readFileSync(this._sessionFile, "utf8").trim();
          if (this._sessionId) {
            console.error(`[cc-bridge] Resume session: ${this._sessionId}`);
          }
        }
      } catch {}
    }

    if (this._sessionId) {
      console.error(`[cc-bridge] Using session: ${this._sessionId}`);
    } else {
      console.error("[cc-bridge] New session (will create on first send)");
    }

    this.emit("ready");
  }

  /**
   * 发送消息给 Claude Code
   * 每次 spawn 独立进程，通过 --session-id 保持上下文
   *
   * @param {string} text - 消息内容
   * @returns {Promise<{text: string, session_id: string, cost?: number}>}
   */
  send(text) {
    return new Promise((resolve, reject) => {
      if (!this._running) {
        reject(new Error("Session not started. Call start() first."));
        return;
      }

      const cleanText = text.replace(/\n/g, " ").trim();
      if (!cleanText) {
        reject(new Error("Empty message"));
        return;
      }

      const args = this._buildArgs(cleanText);
      const isWin = process.platform === "win32";
      const isExe = this._claudeBinary.endsWith(".exe");

      let spawnBinary, spawnArgs;
      if (isWin && isExe) {
        spawnBinary = this._claudeBinary;
        spawnArgs = args;
      } else if (isWin) {
        spawnBinary = "cmd.exe";
        spawnArgs = ["/c", this._claudeBinary, ...args];
      } else {
        spawnBinary = this._claudeBinary;
        spawnArgs = args;
      }

      console.error(`[cc-bridge] >>> ${cleanText.slice(0, 100)}`);

      const startTime = Date.now();
      let responseText = "";
      let sessionId = null;
      let cost = 0;

      const proc = spawn(spawnBinary, spawnArgs, {
        cwd: this.options.projectRoot,
        stdio: ["pipe", "pipe", "pipe"],
        env: { ...process.env, CLAUDE_CODE: "1" },
      });

      // 关闭 stdin（无需输入，prompt 已在参数中）
      proc.stdin.end();

      // 解析 stdout（NDJSON）
      let buffer = "";
      proc.stdout.on("data", (chunk) => {
        buffer += chunk.toString();
        const lines = buffer.split("\n");
        buffer = lines.pop() || "";

        for (const line of lines) {
          const trimmed = line.trim();
          if (!trimmed) continue;
          try {
            const event = JSON.parse(trimmed);
            this._processEvent(event, (partial) => {
              responseText += partial;
              this.emit("text_delta", {
                text: partial,
                session_id: sessionId,
              });
            });
            // 跟踪 sessionId 和 cost
            if (event.session_id) sessionId = event.session_id;
            if (event.total_cost_usd) cost = event.total_cost_usd;
          } catch {}
        }
      });

      // 静默 stderr
      proc.stderr.on("data", () => {});

      // 超时保护
      const timeout = setTimeout(() => {
        proc.kill();
        reject(new Error(`Claude Code 回复超时 (${this.options.timeout}ms)`));
      }, this.options.timeout);

      proc.on("exit", (code) => {
        clearTimeout(timeout);

        // 处理 buffer 中可能剩余的行
        if (buffer.trim()) {
          try {
            const event = JSON.parse(buffer.trim());
            this._processEvent(event, (partial) => {
              responseText += partial;
            });
            if (event.session_id) sessionId = event.session_id;
            if (event.total_cost_usd) cost = event.total_cost_usd;
          } catch {}
        }

        // 保存 session ID
        if (sessionId) {
          this._sessionId = sessionId;
          try { fs.writeFileSync(this._sessionFile, sessionId, "utf8"); } catch {}
        }

        const elapsed = Date.now() - startTime;
        console.error(`[cc-bridge] <<< ${elapsed}ms cost=$${cost.toFixed(4)}`);

        if (code === 0 && responseText) {
          this.emit("response", { text: responseText, session_id: sessionId });
          resolve({ text: responseText, session_id: sessionId, cost });
        } else if (code !== 0 && !responseText) {
          reject(new Error(`Claude Code 退出 (code=${code})`));
        } else {
          // 有响应但退出码非0，仍返回响应
          this.emit("response", { text: responseText, session_id: sessionId });
          resolve({ text: responseText, session_id: sessionId, cost });
        }
      });

      proc.on("error", (err) => {
        clearTimeout(timeout);
        reject(new Error(`启动失败: ${err.message}`));
      });
    });
  }

  /** 解析 JSON 事件，提取文本 */
  _processEvent(event, onDelta) {
    if (event.type === "assistant") {
      const msg = event.message;
      if (msg && msg.content) {
        for (const block of msg.content) {
          if (block.type === "text" && block.text) {
            onDelta(block.text);
          } else if (block.type === "thinking" && block.thinking) {
            this.emit("thinking", { text: block.thinking });
          } else if (block.type === "tool_use") {
            this.emit("tool_use", { name: block.name, input: block.input });
          }
        }
      }
    } else if (event.type === "result") {
      if (event.is_error) {
        this.emit("error", { message: event.result || "Unknown error" });
      }
      // permission_denials
      if (event.permission_denials && event.permission_denials.length > 0) {
        this.emit("permission", { denials: event.permission_denials });
      }
      this.emit("done", {
        usage: event.usage,
        cost: event.total_cost_usd,
        stop_reason: event.stop_reason,
      });
    }
  }

  /**
   * 获取当前状态
   */
  getStatus() {
    return {
      running: this._running,
      session_id: this._sessionId,
      claude: this._claudeBinary,
    };
  }

  /**
   * 停止
   */
  stop() {
    this._running = false;
  }
}

module.exports = { ClaudeCodeSession };
