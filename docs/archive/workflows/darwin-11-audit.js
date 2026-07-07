export const meta = {
  name: 'darwin-11-audit',
  description: '达尔文11.0 — 全项目全面审计优化(含7维度并行审计+交叉验证+修复+验证)',
  phases: [
    { title: 'Deep Audit', detail: '7维度并行深度审计' },
    { title: 'Cross-Ref', detail: '跨文件交叉引用审计' },
    { title: 'Synthesize', detail: '汇总发现并生成修复方案' },
    { title: 'Fix', detail: '并行应用修复' },
    { title: 'Verify', detail: '运行知识库检查并验证' },
  ],
}

// ── Shared schema ──────────────────────────────────────────────
const FINDING = {
  type: 'object',
  properties: {
    file: { type: 'string' },
    line: { type: 'number' },
    severity: { type: 'string', enum: ['CRITICAL', 'HIGH', 'MEDIUM', 'LOW', 'INFO'] },
    category: { type: 'string' },
    issue: { type: 'string' },
    fix: { type: 'string' },
    autoFix: { type: 'boolean' },
  },
  required: ['file', 'issue', 'severity', 'fix'],
}
const SCHEMA = {
  type: 'object',
  properties: { findings: { type: 'array', items: FINDING } },
  required: ['findings'],
}

// ── Phase 1: Deep Audit (7 parallel agents) ────────────────────
phase('Deep Audit')
log('启动7维度并行深度审计...')

const [
  skillAudit,
  scriptAudit,
  knowledgeAudit,
  configAudit,
  claudeMdAudit,
  webuiAudit,
  ghActionsAudit,
] = await parallel([
  () => agent(`
    Deep audit of ALL 8 SKILL.md files under skills/. Check every file:
    - frontmatter: name, description, model fields
    - D3 count and content quality
    - D4 count and content quality (are CHECKPOINTs actionable?)
    - D9 count and content quality
    - Does SKILL reference files that exist?
    - Theory/methodology section present?
    - PushPlus boilerplate duplication
    - test-prompts.json exists
    - Cross-references to other agents

    Return ALL findings, even minor ones.
    Project: C:\\Users\\65004\\Desktop\\小白\\股票投资
  `, { label: 'SKILL Audit', phase: 'Deep Audit', schema: SCHEMA, model: 'opus' }),

  () => agent(`
    Deep audit of ALL Python scripts under scripts/ (excluding __pycache__).
    Check for:
    - Bare except/pass (silent error swallowing)
    - Hardcoded absolute paths (C:\\\\...)
    - Hardcoded dates that will expire
    - Unused imports/variables
    - Improper error handling (generic except without logging)
    - D3/D4/D9 implementations - do they match the docstring?
    - Any scripts that call non-existent functions or reference non-existent files
    - Scripts without proper __main__ guard

    Project: C:\\Users\\65004\\Desktop\\小白\\股票投资
  `, { label: 'Script Audit', phase: 'Deep Audit', schema: SCHEMA, model: 'opus' }),

  () => agent(`
    Deep audit of ALL knowledge base files.
    Check for:
    - INDEX.md: every link works, no orphan .md files
    - CHANGES.md: path convention consistency, file existence
    - Wikilinks: all [[wikilinks]] resolve (not just the obvious ones; grep for ALL [[ patterns including ones inside code blocks)
    - Strategy files: D3/D4/D9 consistency, cross-references, last-updated accuracy
    - Review records: schema consistency, completeness
    - LLM-Wiki-工作法.md: references to pages that don't exist
    - Any knowledge file that references data/ scripts/ or config files that don't exist

    Project: C:\\Users\\65004\\Desktop\\小白\\股票投资
  `, { label: 'Knowledge Audit', phase: 'Deep Audit', schema: SCHEMA, model: 'opus' }),

  () => agent(`
    Deep audit of data/ config files and project configuration.
    Check:
    - data/选股规则.json: 8 factor weights sum to 100 for each mode, scoring_profile validity
    - data/仓位管理规则.json: cross-ref with 止损规则.json offsets
    - data/止损规则.json: 8 rules, naming convention
    - data/策略规则.json: 8-factor alignment
    - data/portfolio.json: schema consistency, JSON validity
    - data/watchlist.json: schema validity
    - data/trading_calendar.json: non-empty
    - .claude/settings.json: hook configuration correctness
    - .mcp.json: server definitions
    - cc-connect.toml: configuration validity
    - .env.example: completeness vs actual usage

    Project: C:\\Users\\65004\\Desktop\\小白\\股票投资
  `, { label: 'Config Audit', phase: 'Deep Audit', schema: SCHEMA }),

  () => agent(`
    Deep audit of CLAUDE.md against actual project state.
    Check:
    - Every file path mentioned in CLAUDE.md must exist
    - D3/D4/D9 counts per agent must match actual SKILL.md files
    - Agent model assignments must match SKILL frontmatter
    - Directory structure must be accurate
    - All referenced schedule times must match scheduled_tasks.json
    - All script paths must exist
    - Agent count (8 vs documented 7 + 问股)
    - Darwin optimization history accuracy

    Project: C:\\Users\\65004\\Desktop\\小白\\股票投资
  `, { label: 'CLAUDE.md Audit', phase: 'Deep Audit', schema: SCHEMA, model: 'opus' }),

  () => agent(`
    Audit the WebUI at webui/.
    Check:
    - main.py: imports and routes
    - routers/: all agent routes exist and match agent names
    - services/: service layer consistency
    - Any missing routes or broken imports
    - File existence for all referenced paths

    Project: C:\\Users\\65004\\Desktop\\小白\\股票投资
  `, { label: 'WebUI Audit', phase: 'Deep Audit', schema: SCHEMA }),

  () => agent(`
    Audit .github/workflows/00-daily-analysis.yml and memory/ files.
    For GitHub Actions:
    - Syntax correctness
    - Step ordering (sync before analysis)
    - Token/secret usage matches .env.example
    - Duplication with local cron jobs
    - Timeout settings
    - Artifact retention

    For memory/ files:
    - All listed in MEMORY.md exist
    - Cross-references to project files are valid

    Project: C:\\Users\\65004\\Desktop\\小白\\股票投资
  `, { label: 'GHA+Memory Audit', phase: 'Deep Audit', schema: SCHEMA }),
])

// ── Phase 2: Cross-Reference Audit ────────────────────────────
phase('Cross-Ref')
log('执行跨文件交叉引用审计...')

const crossRefFindings = await agent(`
  Cross-reference audit of the entire project.

  Check:
  1. SKILL → Knowledge references: For each SKILL.md, check that every knowledge/ file it references actually exists
  2. SKILL → Data references: For each SKILL.md, check that every data/ file it references actually exists
  3. SKILL → Script references: For each SKILL.md, check that every scripts/ file it references actually exists
  4. Knowledge → Data references: For each knowledge/ file, check that every data/ file it references actually exists
  5. Knowledge → SKILL references: For each knowledge/ file, check that every skills/ file it references actually exists
  6. CLAUDE.md → All: verify every file path in CLAUDE.md exists
  7. Agent cross-reference coverage: which agents reference which knowledge files? Are there gaps?
  8. Script → Data references: check that every data/ file path in scripts exists

  Known gaps to verify:
  - agent3 (风控官) should reference 交易执行规则.md but doesn't
  - agent6 (操盘手) should reference 选股策略.md but doesn't
  - agent-问股 doesn't reference agent6 or agent1
  - agent4 missing academic framework name

  Project: C:\\Users\\65004\\Desktop\\小白\\股票投资
`, { label: 'Cross-Reference Audit', phase: 'Cross-Ref', schema: SCHEMA, model: 'opus' })

// ── Phase 3: Synthesis ─────────────────────────────────────────
phase('Synthesize')
log('汇总所有发现，生成修复方案...')

const allFindings = [
  ...(skillAudit?.findings || []),
  ...(scriptAudit?.findings || []),
  ...(knowledgeAudit?.findings || []),
  ...(configAudit?.findings || []),
  ...(claudeMdAudit?.findings || []),
  ...(webuiAudit?.findings || []),
  ...(ghActionsAudit?.findings || []),
  ...(crossRefFindings?.findings || []),
]

// Extended schema for synthesis output
const SCHEMA_EXT = {
  type: 'object',
  properties: {
    findings: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          id: { type: 'string' },
          severity: { type: 'string', enum: ['CRITICAL', 'HIGH', 'MEDIUM', 'LOW', 'INFO'] },
          category: { type: 'string' },
          file: { type: 'string' },
          line: { type: 'number' },
          issue: { type: 'string' },
          fix: { type: 'string' },
          autoFix: { type: 'boolean' },
          editInstructions: { type: 'string' },
        },
        required: ['id', 'severity', 'category', 'file', 'issue', 'fix', 'autoFix'],
      },
    },
    summary: {
      type: 'object',
      properties: {
        total: { type: 'number' },
        critical: { type: 'number' },
        high: { type: 'number' },
        medium: { type: 'number' },
        low: { type: 'number' },
        info: { type: 'number' },
      },
      required: ['total'],
    },
  },
  required: ['findings', 'summary'],
}

const synthesis = await agent(`
  You are the synthesis agent for Darwin 11.0 audit. Aggregate and prioritize findings.

  RAW FINDINGS (${allFindings.length} total):
  ${JSON.stringify(allFindings, null, 2)}

  Tasks:
  1. Deduplicate findings (same issue reported by multiple auditors)
  2. Merge related findings into consolidated items
  3. Prioritize: CRITICAL > HIGH > MEDIUM > LOW > INFO
  4. For each finding, determine if it can be auto-fixed (simple text replacement)
  5. Group into fix categories:
     - CAT_A_SKILL: SKILL.md frontmatter/formatting fixes
     - CAT_B_SCRIPT: Python script bug fixes (except:pass, hardcoded paths, etc.)
     - CAT_C_KNOWLEDGE: Knowledge base fixes
     - CAT_D_CONFIG: Config/data file fixes
     - CAT_E_CLAUDE: CLAUDE.md fixes
     - CAT_F_CROSSREF: Cross-reference fixes
  6. Generate a prioritized fix plan with specific file paths and edit instructions

  Output format matches SCHEMA_EXT schema with fields: id, severity, category, file, line, issue, fix, autoFix, editInstructions
  Also include summary with total, critical, high, medium, low, info counts.
`, { label: 'Synthesize Findings', phase: 'Synthesize', schema: SCHEMA_EXT, model: 'opus' })

log(`合成完成：CRITICAL=${synthesis.summary.critical}, HIGH=${synthesis.summary.high}, MEDIUM=${synthesis.summary.medium}, LOW=${synthesis.summary.low}, INFO=${synthesis.summary.info}`)

// ── Phase 4: Fix Application ────────────────────────────────────
phase('Fix')
log('按类别并行应用修复...')

// Group findings by fix category
const fixGroups = {}
for (const f of synthesis.findings) {
  if (!f.autoFix) continue
  const cat = f.category
  if (!fixGroups[cat]) fixGroups[cat] = []
  fixGroups[cat].push(f)
}

log(`可自动修复发现：${Object.values(fixGroups).flat().length}/${synthesis.summary.total}`)

const fixResults = await parallel(Object.entries(fixGroups).map(([cat, findings]) => () =>
  agent(`
    Apply these fixes for category ${cat}. Use the Edit tool for each fix.

    Findings to fix:
    ${JSON.stringify(findings, null, 2)}

    For each finding:
    1. Read the file first
    2. Use Edit to apply the fix
    3. Report success/failure

    Return:
    {
      "category": "${cat}",
      "applied": number (how many fixes succeeded),
      "failed": number,
      "details": [{"id": "Fxxx", "status": "applied|failed|skipped", "reason": "..."}]
    }
  `, {
    label: `Fix: ${cat}`,
    phase: 'Fix',
    schema: {
      type: 'object',
      properties: {
        category: { type: 'string' },
        applied: { type: 'number' },
        failed: { type: 'number' },
        details: {
          type: 'array',
          items: {
            type: 'object',
            properties: {
              id: { type: 'string' },
              status: { type: 'string' },
              reason: { type: 'string' },
            },
            required: ['id', 'status'],
          },
        },
      },
      required: ['category', 'applied', 'failed'],
    },
    model: 'opus',
  })
))

// ── Phase 5: Verification ──────────────────────────────────────
phase('Verify')
log('运行验证...')

const verifyResult = await agent(`
  Verify the Darwin 11.0 fixes. Do the following:

  1. Run: python scripts/utils/knowledge_lint.py
  2. Check that the knowledge base health report shows 🟢 GREEN
  3. Verify that no broken references remain
  4. Check git status to see what files were modified
  5. Report the final state

  Also check:
  - All SKILL.md files now have model field in frontmatter (if applicable)
  - No hardcoded C: paths remain
  - CHANGES.md path prefixes are correct

  Project: C:\\Users\\65004\\Desktop\\小白\\股票投资

  Return:
  {
    "knowledgeLint": "success|issues",
    "lintOutput": "summary of lint results",
    "filesModified": number,
    "fixesApplied": number,
    "fixesFailed": number,
    "healthStatus": "GREEN|YELLOW|RED",
    "unresolvedCount": number
  }
`, {
  label: 'Verify Fixes',
  phase: 'Verify',
  schema: {
    type: 'object',
    properties: {
      knowledgeLint: { type: 'string' },
      lintOutput: { type: 'string' },
      filesModified: { type: 'number' },
      fixesApplied: { type: 'number' },
      fixesFailed: { type: 'number' },
      healthStatus: { type: 'string' },
      unresolvedCount: { type: 'number' },
    },
    required: ['knowledgeLint', 'healthStatus'],
  },
  model: 'opus',
})

// ── Final Report ────────────────────────────────────────────────
const appliedTotal = fixResults.filter(Boolean).reduce((s, r) => s + r.applied, 0)
const failedTotal = fixResults.filter(Boolean).reduce((s, r) => s + r.failed, 0)

log(`
══════════════════════════════════════════
  达尔文11.0 — 全项目审计优化 完成报告
══════════════════════════════════════════

  📊 发现统计
    总计: ${synthesis.summary.total}
    CRITICAL: ${synthesis.summary.critical}
    HIGH: ${synthesis.summary.high}
    MEDIUM: ${synthesis.summary.medium}
    LOW: ${synthesis.summary.low}
    INFO: ${synthesis.summary.info}

  🔧 修复统计
    已修复: ${appliedTotal}
    失败: ${failedTotal}

  ✅ 验证状态
    知识库健康度: ${verifyResult.healthStatus}
    PC 知识库检查: ${verifyResult.knowledgeLint}
    修改文件数: ${verifyResult.filesModified}
    未解决发现: ${verifyResult.unresolvedCount}

  📋 未修复发现列表
  ${synthesis.findings.filter(f => !f.autoFix).map(f => `  [${f.severity}] ${f.issue} (${f.file})`).join('\n')}
`)

return {
  summary: synthesis.summary,
  fixesApplied: appliedTotal,
  fixesFailed: failedTotal,
  healthStatus: verifyResult.healthStatus,
  unresolvedFindings: synthesis.findings.filter(f => !f.autoFix).length,
  criticalFindings: synthesis.findings.filter(f => f.severity === 'CRITICAL').map(f => ({ issue: f.issue, file: f.file })),
  highFindings: synthesis.findings.filter(f => f.severity === 'HIGH').map(f => ({ issue: f.issue, file: f.file })),
}
