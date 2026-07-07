export const meta = {
  name: 'darwin-13-health-check',
  description: '达尔文13.0 — 项目全维度健康度快速检查+配置审计(基于Claude Code hooks/agents/settings最佳实践)',
  phases: [
    { title: 'Config Audit', detail: '审计settings/hooks/agents/agents配置' },
    { title: 'Knowledge Check', detail: '知识库健康度检查' },
    { title: 'Synthesize', detail: '汇总发现' },
  ],
}

const FINDING = {
  type: 'object',
  properties: {
    severity: { type: 'string', enum: ['CRITICAL', 'HIGH', 'MEDIUM', 'LOW', 'INFO'] },
    category: { type: 'string' },
    file: { type: 'string' },
    issue: { type: 'string' },
    fix: { type: 'string' },
  },
  required: ['severity', 'category', 'file', 'issue', 'fix'],
}
const SCHEMA = {
  type: 'object',
  properties: { findings: { type: 'array', items: FINDING } },
  required: ['findings'],
}

phase('Config Audit')
log('并行审计settings/hooks/agents配置...')

const [settingsAudit, hooksAudit, agentsAudit, claudeMdAudit] = await parallel([
  () => agent(`
    Audit .claude/settings.json and .claude/settings.local.json.
    Check:
    - hooks: SessionStart, PreToolUse, Stop, PreCompact all present?
    - permissions: are common operations covered?
    - model: set appropriately?
    - effortLevel: set?
    - Any missing best-practice settings
    Project: C:\\Users\\65004\\Desktop\\小白\\股票投资
  `, { label: 'Settings Audit', phase: 'Config Audit', schema: SCHEMA }),

  () => agent(`
    Audit all hook scripts in .claude/hooks/.
    Check:
    - Do all scripts exist and match settings.json references?
    - Do they have proper error handling?
    - Are timeouts appropriate?
    - Do they follow best practices (idempotent, graceful degradation)?
    - Any security issues?
    Project: C:\\Users\\65004\\Desktop\\小白\\股票投资
  `, { label: 'Hooks Audit', phase: 'Config Audit', schema: SCHEMA }),

  () => agent(`
    Audit .claude/agents/ directory.
    Check:
    - Each .md file has valid frontmatter (name, description, model, tools)
    - Agent definitions reference real agent types
    - permissionMode is set appropriately
    - No duplicate names
    Project: C:\\Users\\65004\\Desktop\\小白\\股票投资
  `, { label: 'Agents Audit', phase: 'Config Audit', schema: SCHEMA }),

  () => agent(`
    Quick audit of CLAUDE.md for accuracy.
    Check:
    - All file paths referenced exist
    - memory/ description is accurate (two locations: project-root memory/ and user persistent memory)
    - Agent count matches actual skills/ count
    - Schedule times match scheduled_tasks.json
    - Recent Darwin history entries present
    Project: C:\\Users\\65004\\Desktop\\小白\\股票投资
  `, { label: 'CLAUDE.md Audit', phase: 'Config Audit', schema: SCHEMA }),
])

phase('Knowledge Check')
log('运行知识库健康度检查...')

const lintResult = await agent(`
  Run knowledge_lint and report results.
  Run: python scripts/utils/knowledge_lint.py
  Also check:
  - knowledge/CHANGES.md is up to date
  - knowledge/INDEX.md cross-references are current
  - All [[wikilinks]] in knowledge/ files resolve
  Project: C:\\Users\\65004\\Desktop\\小白\\股票投资
  Return: { healthStatus: "GREEN|YELLOW|RED", issues: number, report: "summary" }
`, {
  label: 'Knowledge Lint',
  phase: 'Knowledge Check',
  schema: {
    type: 'object',
    properties: {
      healthStatus: { type: 'string' },
      issues: { type: 'number' },
      report: { type: 'string' },
    },
    required: ['healthStatus', 'issues'],
  },
})

phase('Synthesize')
log('汇总所有发现...')

const allFindings = [
  ...(settingsAudit?.findings || []),
  ...(hooksAudit?.findings || []),
  ...(agentsAudit?.findings || []),
  ...(claudeMdAudit?.findings || []),
]

const critical = allFindings.filter(f => f.severity === 'CRITICAL').length
const high = allFindings.filter(f => f.severity === 'HIGH').length
const medium = allFindings.filter(f => f.severity === 'MEDIUM').length
const low = allFindings.filter(f => f.severity === 'LOW').length

log(`
══════════════════════════════════════════
  达尔文13.0 — 健康度检查完成报告
══════════════════════════════════════════

  📊 配置审计发现
    CRITICAL: ${critical}  HIGH: ${high}  MEDIUM: ${medium}  LOW: ${low}

  ✅ 知识库健康度: ${lintResult?.healthStatus || '未知'}
  📝 报告: ${lintResult?.report || ''}

  📋 发现详情:
  ${allFindings.map(f => `  [${f.severity}] ${f.issue} (${f.file})`).join('\n') || '  无'}
`)

return {
  configFindings: {
    total: allFindings.length,
    critical,
    high,
    medium,
    low,
    findings: allFindings,
  },
  knowledgeHealth: lintResult?.healthStatus || 'UNKNOWN',
}
