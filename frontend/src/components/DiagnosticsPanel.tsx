import { ArrowUpRight, BrainCircuit, RotateCcw, ShieldAlert, SquareTerminal } from 'lucide-react'
import { useMemo, useState } from 'react'

import type { IntentExecution, PlanningAttempt, ProjectSnapshot } from '../types'

type AuditTab = 'execution' | 'planning'
type TimeFilter = 'all' | 'hour' | 'day' | 'week'

const statusLabel: Record<string, string> = {
  planned: '排队中',
  queued: '排队中',
  running: '运行中',
  completed: '已完成',
  failed: '失败',
  protocol_error: '协议错误',
  interrupted: '重启中断',
  cancelled: '已取消',
  timeout: '超时',
  idle: '无任务',
  stale: '计划过期',
  valid: '已生成 Intent',
  healthy: '健康',
  unhealthy: '不可用',
  auth_failed: '认证失败',
  missing: '命令缺失',
  cooldown: '冷却中',
  unknown: '未探测',
}

const retryable = new Set(['failed', 'protocol_error', 'interrupted', 'cancelled', 'timeout'])

function withinTime(value: string | null | undefined, filter: TimeFilter) {
  if (filter === 'all') return true
  if (!value) return false
  const timestamp = Date.parse(value)
  if (!Number.isFinite(timestamp)) return false
  const windowMs = filter === 'hour' ? 3600000 : filter === 'day' ? 86400000 : 604800000
  return Date.now() - timestamp <= windowMs
}

function shortTime(value: string | null | undefined) {
  if (!value) return '—'
  const timestamp = Date.parse(value)
  return Number.isFinite(timestamp)
    ? new Intl.DateTimeFormat('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }).format(timestamp)
    : value
}

export function DiagnosticsPanel({
  snapshot,
  onSelectExecution,
  onSelectPlanning,
  onSelectIntent,
  onRetry,
}: {
  snapshot: ProjectSnapshot
  onSelectExecution: (execution: IntentExecution) => void
  onSelectPlanning: (attempt: PlanningAttempt) => void
  onSelectIntent: (intentId: string) => void
  onRetry: (execution: IntentExecution) => void
}) {
  const [tab, setTab] = useState<AuditTab>('execution')
  const [provider, setProvider] = useState('all')
  const [status, setStatus] = useState('all')
  const [time, setTime] = useState<TimeFilter>('all')
  const executions = useMemo(() => snapshot.edges.flatMap(edge => edge.executions ?? []), [snapshot.edges])
  const providers = useMemo(() => Array.from(new Set([
    ...executions.map(item => item.provider),
    ...snapshot.planning_attempts.map(item => item.provider),
  ])).sort(), [executions, snapshot.planning_attempts])
  const records = tab === 'execution' ? executions : snapshot.planning_attempts
  const statuses = Array.from(new Set(records.map(item => item.status))).sort()
  const filtered = records.filter(item => (
    (provider === 'all' || item.provider === provider)
    && (status === 'all' || item.status === status)
    && withinTime(item.queued_at ?? item.started_at, time)
  ))

  function changeTab(next: AuditTab) {
    setTab(next)
    setStatus('all')
  }

  return (
    <aside className="inspector diagnostics-panel" aria-label="运行诊断">
      <div className="inspector-heading">
        <div>
          <span className="inspector-kicker">OBSERVABILITY</span>
          <h2>运行诊断</h2>
        </div>
        <ShieldAlert size={18} />
      </div>

      <section className="diagnostic-health">
        {snapshot.provider_health.map(health => (
          <div key={health.provider}>
            <strong>{health.provider}</strong>
            <span className={`health-chip health-chip--${health.status}`}>{statusLabel[health.status] ?? health.status}</span>
            {health.last_error && <small title={health.last_error}>{health.last_error}</small>}
          </div>
        ))}
        {snapshot.provider_health.length === 0 && <p className="diagnostic-empty">未配置 Provider</p>}
      </section>

      <div className="diagnostic-tabs" role="tablist" aria-label="审计记录类型">
        <button role="tab" aria-selected={tab === 'execution'} className={tab === 'execution' ? 'is-active' : ''} onClick={() => changeTab('execution')}><SquareTerminal size={15} /> Execution</button>
        <button role="tab" aria-selected={tab === 'planning'} className={tab === 'planning' ? 'is-active' : ''} onClick={() => changeTab('planning')}><BrainCircuit size={15} /> Planning</button>
      </div>

      <div className="diagnostic-filters">
        <label><span>Provider</span><select value={provider} onChange={event => setProvider(event.target.value)}><option value="all">全部</option>{providers.map(value => <option key={value} value={value}>{value}</option>)}</select></label>
        <label><span>状态</span><select value={status} onChange={event => setStatus(event.target.value)}><option value="all">全部</option>{statuses.map(value => <option key={value} value={value}>{statusLabel[value] ?? value}</option>)}</select></label>
        <label><span>时间</span><select value={time} onChange={event => setTime(event.target.value as TimeFilter)}><option value="all">全部</option><option value="hour">1 小时</option><option value="day">24 小时</option><option value="week">7 天</option></select></label>
      </div>

      <div className="audit-records">
        {tab === 'execution' && (filtered as IntentExecution[]).map(execution => (
          <div className="audit-row" key={execution.id}>
            <button className="audit-row-main" onClick={() => onSelectExecution(execution)}>
              <span><strong>{execution.provider}</strong><small>{shortTime(execution.queued_at ?? execution.started_at)}</small></span>
              <span className={`audit-status audit-status--${execution.status}`}>{statusLabel[execution.status] ?? execution.status}</span>
              <code>{execution.id}</code>
            </button>
            {retryable.has(execution.status) && (
              <button className="icon-button" aria-label={`重试 ${execution.id}`} title="创建新的 Execution 重试" onClick={() => onRetry(execution)}><RotateCcw size={15} /></button>
            )}
          </div>
        ))}
        {tab === 'planning' && (filtered as PlanningAttempt[]).map(attempt => (
          <div className="audit-row" key={attempt.id}>
            <button className="audit-row-main" onClick={() => onSelectPlanning(attempt)}>
              <span><strong>{attempt.provider}</strong><small>{shortTime(attempt.queued_at)}</small></span>
              <span className={`audit-status audit-status--${attempt.status}`}>{statusLabel[attempt.status] ?? attempt.status}</span>
              <code>{attempt.id}</code>
            </button>
            {attempt.intent_edge_id && (
              <button className="icon-button" aria-label={`定位 ${attempt.intent_edge_id}`} title="定位产生的 Intent" onClick={() => onSelectIntent(attempt.intent_edge_id!)}><ArrowUpRight size={15} /></button>
            )}
          </div>
        ))}
        {filtered.length === 0 && <p className="diagnostic-empty">没有符合条件的记录。</p>}
      </div>
    </aside>
  )
}
