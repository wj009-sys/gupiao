/**
 * 初始化 3 个定时任务（通过 claw MCP server 写入 scheduled_tasks.json）
 * 运行: node .claude/mcp-servers/claw/setup-tasks.js
 */
const fs = require("fs");
const path = require("path");

const PROJECT_ROOT = path.resolve(__dirname, "..", "..", "..");
const TASKS_FILE = path.join(PROJECT_ROOT, ".claude", "scheduled_tasks.json");

const tasks = [
  {
    id: "claw_agent1_" + Date.now(),
    name: "agent1-情报员",
    cron: "0 7 * * 1-5",
    prompt: `现在是工作日早上7点，执行 Agent1 情报采集任务：
1. 读取 skills/agent1-情报员/SKILL.md 了解工作流程
2. 运行：source venv/Scripts/activate && python -X utf8 scripts/agent1-情报采集/fetch_all.py
3. 用 WebFetch 和 WebSearch 抓取今日财经新闻
4. 按 SKILL.md 模板生成情报摘要报告
5. 保存到 reports/日报/情报/ 目录
6. 输出3-5句话要点汇总给用户`,
    recurring: true,
    createdAt: new Date().toISOString(),
    source: "claw-mcp",
  },
  {
    id: "claw_agent2_" + Date.now(),
    name: "agent2-分析师",
    cron: "30 8 * * 1-5",
    prompt: `现在是工作日早上8点30分，执行 Agent2 技术分析任务：
1. 读取 skills/agent2-分析师/SKILL.md 了解工作流程
2. 读取今早情报报告 reports/日报/情报/（最新）
3. 运行：source venv/Scripts/activate && python -X utf8 scripts/agent2-技术分析/analyze.py
4. 如果板块数据为空，用 WebFetch 补充板块涨跌排名
5. 按 SKILL.md 模板生成分析报告
6. 保存到 reports/日报/分析/ 目录`,
    recurring: true,
    createdAt: new Date().toISOString(),
    source: "claw-mcp",
  },
  {
    id: "claw_agent4_" + Date.now(),
    name: "agent4-复盘师",
    cron: "0 21 * * 1-5",
    prompt: `现在是工作日晚上9点，执行 Agent4 复盘任务：
1. 读取 skills/agent4-复盘师/SKILL.md 了解复盘流程
2. 读取今日情报+分析+风控报告
3. 运行：source venv/Scripts/activate && python -X utf8 scripts/agent4-复盘/review.py
4. 用 WebFetch 搜索今日板块实际涨跌
5. 手动复核板块预测准确性
6. 做偏差深度分析，记录改进措施
7. 按 SKILL.md 模板生成复盘报告
8. 保存到 reports/日报/复盘/ 目录`,
    recurring: true,
    createdAt: new Date().toISOString(),
    source: "claw-mcp",
  },
];

// 读取现有任务，合并
let existing = { tasks: [] };
try {
  existing = JSON.parse(fs.readFileSync(TASKS_FILE, "utf-8"));
} catch {}
existing.tasks = existing.tasks.filter((t) => !t.id.startsWith("claw_"));
existing.tasks.push(...tasks);

fs.mkdirSync(path.dirname(TASKS_FILE), { recursive: true });
fs.writeFileSync(TASKS_FILE, JSON.stringify(existing, null, 2), "utf-8");
console.log(`已写入 ${tasks.length} 个 claw 定时任务到 ${TASKS_FILE}`);
console.log(`当前任务总数: ${existing.tasks.length}`);
tasks.forEach((t) => console.log(`  ${t.name}: ${t.cron}`));
