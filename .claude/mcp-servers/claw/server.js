#!/usr/bin/env node
/**
 * claw MCP Server — 定时调度工具
 *
 * 提供工具:
 *   - cron: 创建持久化的定时任务（存储在 .claude/scheduled_tasks.json）
 *
 * MCP 协议: stdio JSON-RPC
 */

const fs = require("fs");
const path = require("path");
const { once } = require("events");
const { CronExpressionParser } = require("cron-parser");

// ============ 工具定义 ============

const TOOLS = [
  {
    name: "cron",
    description: "创建持久化的 cron 定时任务。支持标准的 5 字段 cron 表达式。任务会被持久化存储，跨会话保持。",
    inputSchema: {
      type: "object",
      properties: {
        cron: {
          type: "string",
          description: "标准5字段cron表达式（分 时 日 月 周），例如: '0 7 * * 1-5' = 工作日7点",
        },
        prompt: {
          type: "string",
          description: "定时触发时执行的 prompt 内容",
        },
        name: {
          type: "string",
          description: "任务名称（可选，不传则自动生成）",
        },
        recurring: {
          type: "boolean",
          description: "是否重复执行，默认 true",
          default: true,
        },
      },
      required: ["cron", "prompt"],
    },
  },
  {
    name: "cron_list",
    description: "列出所有已注册的 cron 定时任务",
    inputSchema: {
      type: "object",
      properties: {},
    },
  },
  {
    name: "cron_delete",
    description: "删除指定的 cron 定时任务",
    inputSchema: {
      type: "object",
      properties: {
        id: {
          type: "string",
          description: "任务 ID（从 cron_list 获取）",
        },
      },
      required: ["id"],
    },
  },
];

// ============ 任务存储 ============

function getTasksPath() {
  // 向上查找项目根目录的 .claude/scheduled_tasks.json
  let dir = __dirname;
  for (let i = 0; i < 10; i++) {
    const candidate = path.join(dir, ".claude", "scheduled_tasks.json");
    if (fs.existsSync(candidate)) return candidate;
    const parent = path.dirname(dir);
    if (parent === dir) break;
    dir = parent;
  }
  // 回退到当前目录的 .claude/
  const fallback = path.join(__dirname, ".claude", "scheduled_tasks.json");
  fs.mkdirSync(path.dirname(fallback), { recursive: true });
  return fallback;
}

function loadTasks() {
  const p = getTasksPath();
  try {
    return JSON.parse(fs.readFileSync(p, "utf-8"));
  } catch {
    return { tasks: [] };
  }
}

function saveTasks(data) {
  const p = getTasksPath();
  fs.mkdirSync(path.dirname(p), { recursive: true });
  fs.writeFileSync(p, JSON.stringify(data, null, 2), "utf-8");
}

// ============ MCP 协议处理 ============

async function handleMessage(msg) {
  const { id, method, params } = msg;

  switch (method) {
    // ---- 初始化 ----
    case "initialize":
      return {
        id,
        result: {
          protocolVersion: "2024-11-05",
          capabilities: {
            tools: {},
          },
          serverInfo: {
            name: "claw",
            version: "1.0.0",
          },
        },
      };

    case "notifications/initialized":
      return null; // no response

    // ---- 工具列表 ----
    case "tools/list":
      return { id, result: { tools: TOOLS } };

    // ---- 工具调用 ----
    case "tools/call": {
      const toolName = params.name;
      const args = params.arguments || {};

      switch (toolName) {
        case "cron": {
          const { cron, prompt, name, recurring = true } = args;
          if (!cron || !prompt) {
            return {
              id,
              error: { code: -32602, message: "缺少必填参数: cron, prompt" },
            };
          }

          // 验证 cron 表达式
          try {
            CronExpressionParser.parse(cron);
          } catch (e) {
            return {
              id,
              error: { code: -32602, message: `无效的 cron 表达式: ${e.message}` },
            };
          }

          const data = loadTasks();
          const taskId = `claw_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`;
          const taskName = name || `task_${taskId.slice(0, 10)}`;

          data.tasks.push({
            id: taskId,
            name: taskName,
            cron,
            prompt,
            recurring,
            createdAt: new Date().toISOString(),
            source: "claw-mcp",
          });
          saveTasks(data);

          return {
            id,
            result: {
              content: [
                {
                  type: "text",
                  text: JSON.stringify({
                    success: true,
                    taskId,
                    message: `定时任务已创建: ${cron} → ${prompt.slice(0, 50)}...`,
                  }),
                },
              ],
            },
          };
        }

        case "cron_list": {
          const data = loadTasks();
          return {
            id,
            result: {
              content: [
                {
                  type: "text",
                  text: JSON.stringify(
                    {
                      total: data.tasks.length,
                      tasks: data.tasks.map((t) => ({
                        id: t.id,
                        name: t.name,
                        cron: t.cron,
                        prompt: t.prompt.slice(0, 80),
                        recurring: t.recurring,
                        createdAt: t.createdAt,
                      })),
                    },
                    null,
                    2
                  ),
                },
              ],
            },
          };
        }

        case "cron_delete": {
          const { id: taskId } = args;
          const data = loadTasks();
          const before = data.tasks.length;
          data.tasks = data.tasks.filter((t) => t.id !== taskId);
          if (data.tasks.length === before) {
            return {
              id,
              error: { code: -32602, message: `任务 ${taskId} 未找到` },
            };
          }
          saveTasks(data);
          return {
            id,
            result: {
              content: [{ type: "text", text: JSON.stringify({ success: true, message: `任务 ${taskId} 已删除` }) }],
            },
          };
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
  const taskFiles = getTasksPath();
  console.error(`[claw] MCP server started. Tasks file: ${taskFiles}`);

  // 从 stdin 读取 JSON-RPC 消息
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
        console.error(`[claw] parse error: ${err.message}`);
        // 忽略格式错误的行
      }
    }
  }
}

main().catch((err) => {
  console.error(`[claw] fatal: ${err.message}`);
  process.exit(1);
});
