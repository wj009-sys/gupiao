export const meta = {
  name: "darwin-15-audit",
  description: "达尔文15.0 — 全项目深度优化+全面审计(6维度并行)",
  phases: [
    { title: "DB审计", detail: "24表Schema+索引+空值+OBV列" },
    { title: "Agent脚本审计", detail: "9个Agent脚本字段匹配+数据流" },
    { title: "知识库+数据源审计", detail: "知识库一致性+数据源稳定性" },
    { title: "综合报告", detail: "交叉验证+优先级排序+修复方案" },
  ],
};

var FINDING_SCHEMA = {
  type: "object",
  properties: {
    findings: {
      type: "array",
      items: {
        type: "object",
        properties: {
          id: { type: "string" },
          severity: { type: "string", enum: ["CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO"] },
          domain: { type: "string" },
          file: { type: "string" },
          line: { type: "string" },
          issue: { type: "string" },
          fix: { type: "string" },
        },
        required: ["id", "severity", "domain", "issue", "fix"],
      },
    },
  },
};

var ALL_FINDINGS = [];
var _counter = 1;
function _id(prefix) { return prefix + "-" + (_counter++); }

// ══════════════════════════════════════════════════════════════════
// Phase 1: 并行6维度发现
// ══════════════════════════════════════════════════════════════════

phase("DB审计");

var dbSchema = await agent(
  [
    "你是一个SQLite数据库架构审计专家。审计A股投研数据库(data/stocks.db)的Schema。",
    "",
    "已知问题（需要确认修复状态）:",
    "1. daily_indicator的obv/obv_ma20/obv_trend/obv_divergence 4列 100% NULL（Darwin14新增但未填充）",
    "2. daily_basic的pe/pe_ttm/pb/total_mv/circ_mv/turnover_rate/volume_ratio 7列全为NULLABLE",
    "3. moneyflow_mkt只有1行（严重不足）",
    "4. moneyflow_stock只有199行（严重不足）",
    "5. policy_events 0行（Agent8未运行）",
    "6. daily_price有153,288行涨跌停标记（仅0.8%）",
    "",
    "请审计以下方面:",
    "1. NOT NULL约束: 哪些业务关键列应该加NOT NULL但没加？",
    "2. 索引策略: 哪些表缺少查询需要的索引？（考虑每个Agent的查询模式）",
    "3. 数据类型: 有无类型不合适的列？（如金额用REAL而非INTEGER）",
    "4. 空值风险: 关键业务列允许NULL会对Agent造成什么影响？",
    "5. 改进建议: 表结构优化建议（新增列、拆分大表、添加默认值）",
    "",
    "输出JSON格式的结构化发现（含severity/domain/file/issue/fix）。",
    "domain字段填\"DB_Schema\"。如果某列不需要改动，就不要报告。只报告真正需要修复的问题。",
  ].join("\n"),
  { label: "DB Schema审计", schema: FINDING_SCHEMA }
);

if (dbSchema && dbSchema.findings) {
  dbSchema.findings.forEach(function(f) { f.id = _id("DB"); ALL_FINDINGS.push(f); });
  log("DB Schema审计: " + dbSchema.findings.length + " 项发现");
}

var obvFix = await agent(
  [
    "分析daily_indicator表OBV列填充问题。",
    "",
    "当前状态:",
    "- daily_indicator有19,610,923行",
    "- obv/obv_ma20/obv_trend/obv_divergence 4列100% NULL",
    "- 这4列是Darwin 14.0新增的，但无人编写填充代码",
    "",
    "请阅读以下文件并分析:",
    "- scripts/utils/technical_analysis.py 中的add_all_indicators()函数有没有OBV计算逻辑",
    "- scripts/utils/db_manager.py 的DAILY_INDICATOR_COLS包含这些列吗",
    "- scripts/agent2-技术分析/analyze.py 是否调用了OBV计算",
    "",
    "策划修复方案: 应该在哪一步填充OBV？在technical_analysis.py还是analyze.py？",
    "",
    "输出JSON格式的结构化发现。domain字段填\"OBV_FILL\"。",
  ].join("\n"),
  { label: "OBV列填充分析", schema: FINDING_SCHEMA }
);

if (obvFix && obvFix.findings) {
  obvFix.findings.forEach(function(f) { f.id = _id("OBV"); ALL_FINDINGS.push(f); });
  log("OBV分析: " + obvFix.findings.length + " 项发现");
}

phase("Agent脚本审计");

var agentFieldAudit = await agent(
  [
    "审计所有Agent脚本的数据库字段引用是否与Schema一致。",
    "",
    "请逐一审计以下Agent脚本的SQL查询字段名与实际DB列名的匹配:",
    "",
    "## Agent1 (scripts/agent1-情报采集/fetch_all.py)",
    "- 读取 stock_basic (ts_code/name/market/industry/list_status/list_date/updated_at)",
    "- 写入 dragon_tiger_detail / hot_money_seats / moneyflow_stock",
    "- 注意: dragon_tiger_detail有buy_seats/sell_seats字段，fetch_all写入什么字段？",
    "",
    "## Agent2 (scripts/agent2-技术分析/analyze.py)",
    "- 读取 daily_price的open/high/low/close/vol/amount",
    "- 写入 daily_indicator (检查字段名是否与add_all_indicators输出匹配)",
    "- 注意: macd_dea还是macd_signal? rsi_6还是rsi_14?",
    "",
    "## Agent3 (scripts/agent3-风控/risk_check.py)",
    "- 读取 daily_price/daily_basic/daily_indicator",
    "- pe/pe_ttm/pb是否处理NULL?",
    "",
    "## Agent5 (scripts/agent5-选股/stock_picker.py)",
    "- 读取 daily_indicator的macd/rsi/ma等",
    "- 使用 fina_indicator的revenue/profit_dedt/revenue_yoy/profit_dedt_yoy/or_yoy",
    "- Darwin14修复了profit_dedt_yoy但需要检查是否彻底",
    "",
    "## Agent6 (scripts/agent6-操盘/trader.py)",
    "- 读取 daily_price/daily_basic判断涨跌停",
    "",
    "## Agent问股 (scripts/agent_ask/ask.py)",
    "- 读取 daily_indicator",
    "- Darwin14已修复macd_signal(原macd_dea)/rsi_14(无rsi_6)",
    "- 检查是否还残留旧字段名",
    "",
    "## Agent8 (scripts/agent8-政策分析/policy_analyst.py)",
    "- 写入 policy_events (检查字段匹配)",
    "",
    "## Agent9 (scripts/agent9-游资追踪/hot_money_tracker.py)",
    "- 读取 dragon_tiger_detail/hot_money_seats",
    "",
    "请逐个文件检查SQL的SELECT/INSERT/UPDATE的字段名，与实际DB Schema对比。",
    "",
    "输出JSON格式的结构化发现。domain字段填\"FIELD_MISMATCH\"。只报告真正不匹配的问题。",
  ].join("\n"),
  { label: "Agent字段审计", schema: FINDING_SCHEMA }
);

if (agentFieldAudit && agentFieldAudit.findings) {
  agentFieldAudit.findings.forEach(function(f) { f.id = _id("FLD"); ALL_FINDINGS.push(f); });
  log("Agent字段审计: " + agentFieldAudit.findings.length + " 项发现");
}

var dataflowAudit = await agent(
  [
    "审计Agent间数据流。",
    "",
    "Agent数据流图:",
    "Agent1(情报) => 情报摘要JSON => Agent2(分析) + Agent7(决策)",
    "Agent1(情报) => 龙虎榜/资金数据 => Agent9(游资)",
    "Agent1(情报) => DB(daily_price/daily_basic) => Agent2/Agent3/Agent5/Agent6",
    "Agent8(政策) => policy_events => Agent7(决策)",
    "Agent9(游资) => dragon_tiger_detail/hot_money_seats => Agent5/Agent6",
    "Agent2(分析) => daily_indicator => Agent3/Agent5/Agent6/Agent问股",
    "Agent5(选股) => 选股建议 => Agent6(操盘) + Agent7(决策)",
    "Agent6(操盘) => 交易计划 => Agent3(风控审查) => Agent7(仲裁)",
    "",
    "请审计:",
    "1. 上下游数据格式一致性: Agent1输出的JSON格式是否被Agent2正确解析？",
    "2. DB作为中间媒介: 一个Agent写入DB的数据是否被另一个Agent正确读取？",
    "3. 时序依赖: Agent2在Agent1完成前运行会怎样？",
    "4. 缺失链路: 应该连接但实际未连接的上下游",
    "5. 冗余链路: 两个Agent做重复工作",
    "6. 知识库闭环: Agent4复盘是否反馈到Agent策略",
    "",
    "输出JSON格式的结构化发现。domain字段填\"DATAFLOW\"。只报告真正的问题。",
  ].join("\n"),
  { label: "数据流审计", schema: FINDING_SCHEMA }
);

if (dataflowAudit && dataflowAudit.findings) {
  dataflowAudit.findings.forEach(function(f) { f.id = _id("DF"); ALL_FINDINGS.push(f); });
  log("数据流审计: " + dataflowAudit.findings.length + " 项发现");
}

phase("知识库+数据源审计");

var knowledgeAudit = await agent(
  [
    "审计知识库一致性。",
    "",
    "### A. knowledge/ 与 data/ 配置一致性",
    "- knowledge/策略/选股策略.md 因子权重 vs data/选股规则.json 权重",
    "- knowledge/策略/交易执行规则.md vs data/仓位管理规则.json",
    "- knowledge/策略/择时策略.md vs data/策略规则.json",
    "",
    "### B. SKILL.md 与实际脚本一致性",
    "- Agent SKILL.md中的D3/D4/D9清单 vs 脚本实际实现",
    "- SKILL.md model字段声明 vs 实际使用",
    "",
    "### C. data/配置文件正确性",
    "- data/选股规则.json: 8因子权重+scoring_profile格式正确？",
    "- data/仓位管理规则.json: 数值合理？",
    "- data/止损规则.json: 条件不冲突？",
    "- data/策略规则.json: 信号定义正确？",
    "",
    "### D. knowledge/CHANGES.md 与 git历史一致性",
    "",
    "关键文件路径:",
    "- knowledge/INDEX.md",
    "- knowledge/策略/*.md",
    "- knowledge/复盘记录/*.md",
    "- data/*.json (选股规则/仓位管理/止损规则/策略规则)",
    "- skills/*/SKILL.md (10个技能文件)",
    "- knowledge/CHANGES.md",
    "",
    "输出JSON格式的结构化发现。domain字段填\"KNOWLEDGE\"。",
  ].join("\n"),
  { label: "知识库审计", schema: FINDING_SCHEMA }
);

if (knowledgeAudit && knowledgeAudit.findings) {
  knowledgeAudit.findings.forEach(function(f) { f.id = _id("KN"); ALL_FINDINGS.push(f); });
  log("知识库审计: " + knowledgeAudit.findings.length + " 项发现");
}

var datasourceAudit = await agent(
  [
    "审计数据源接入质量和稳定性。",
    "",
    "当前数据源矩阵:",
    "",
    "付费/Key数据源:",
    "1. Tushare Pro (主数据源) - 200次/min限流, 宕机时AkShare fallback",
    "",
    "免费数据源 (a-stock-data新接入):",
    "2. 腾讯财经 tencent_quote (qt.gtimg.cn) - 实时PE/PB/市值/涨跌停价,不封IP",
    "3. 同花顺 ths_provider (10jqka.com.cn) - 热点题材/一致预期EPS/人气热榜",
    "4. 东财增强 eastmoney_plus (datacenter-web + push2ex) - 行业排名/概念板块",
    "5. 巨潮 cninfo_sentiment (cninfo.com.cn) - 公告全文/互动易问答",
    "6. 涨停打板 limit_up_board (push2ex) - 涨停池/炸板池/打板情绪",
    "",
    "审计要求:",
    "1. 限流策略: 各脚本间API调用是否共享限流？同一数据源被不同脚本并发调用会超限吗？",
    "2. 优雅降级: 新a-stock-data源的导入都用try/except了吗？fallback链路完整吗？",
    "3. 数据新鲜度: Tushare宕机时AkShare数据新鲜度如何？",
    "4. Token安全: Tushare Token是否有硬编码风险？",
    "5. IP封锁风险: 哪些数据源可能封IP？有代理策略吗？",
    "6. 数据源重复: Tushare和a-stock-data提供相同数据时，优先用哪个？",
    "",
    "输出JSON格式的结构化发现。domain字段填\"DATASOURCE\"。",
  ].join("\n"),
  { label: "数据源审计", schema: FINDING_SCHEMA }
);

if (datasourceAudit && datasourceAudit.findings) {
  datasourceAudit.findings.forEach(function(f) { f.id = _id("DS"); ALL_FINDINGS.push(f); });
  log("数据源审计: " + datasourceAudit.findings.length + " 项发现");
}

// ══════════════════════════════════════════════════════════════════
// Phase 2: 综合报告
// ══════════════════════════════════════════════════════════════════

phase("综合报告");

var severityOrder = { CRITICAL: 0, HIGH: 1, MEDIUM: 2, LOW: 3, INFO: 4 };
ALL_FINDINGS.sort(function(a, b) {
  return (severityOrder[a.severity] || 9) - (severityOrder[b.severity] || 9);
});

var criticalHigh = ALL_FINDINGS.filter(function(f) {
  return f.severity === "CRITICAL" || f.severity === "HIGH";
});

var byDomain = {};
ALL_FINDINGS.forEach(function(f) {
  if (!byDomain[f.domain]) byDomain[f.domain] = [];
  byDomain[f.domain].push(f);
});

var counts = {};
["CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO"].forEach(function(s) {
  counts[s] = ALL_FINDINGS.filter(function(f) { return f.severity === s; }).length;
});

log("==================================================");
log("达尔文15.0 审计报告");
log("==================================================");
log("总计: " + ALL_FINDINGS.length + " 项发现");
log("  CRITICAL: " + counts.CRITICAL + " 项");
log("  HIGH: " + counts.HIGH + " 项");
log("  MEDIUM: " + counts.MEDIUM + " 项");
log("  LOW: " + counts.LOW + " 项");
log("  INFO: " + counts.INFO + " 项");
log("");

Object.keys(byDomain).forEach(function(domain) {
  var list = byDomain[domain];
  log("--- " + domain + " (" + list.length + "项) ---");
  list.filter(function(f) {
    return f.severity === "CRITICAL" || f.severity === "HIGH";
  }).forEach(function(f) {
    log("  [" + f.severity + "] " + f.file + ": " + f.issue);
    log("    => " + f.fix);
  });
});

return {
  total: ALL_FINDINGS.length,
  critical: counts.CRITICAL,
  high: counts.HIGH,
  medium: counts.MEDIUM,
  low: counts.LOW,
  info: counts.INFO,
  byDomain: (function() {
    var o = {};
    Object.keys(byDomain).forEach(function(k) { o[k] = byDomain[k].length; });
    return o;
  })(),
  criticalHigh: criticalHigh.map(function(f) {
    return { id: f.id, severity: f.severity, domain: f.domain, file: f.file, issue: f.issue, fix: f.fix };
  }),
  allFindings: ALL_FINDINGS.map(function(f) {
    return { id: f.id, severity: f.severity, domain: f.domain, file: f.file, line: f.line, issue: f.issue, fix: f.fix };
  }),
};
