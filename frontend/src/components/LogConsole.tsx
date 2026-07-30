import { ChevronDown, ChevronUp, Trash2 } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'

import { api } from '../api/client'
import { capLogText } from '../state/projectStore'
import type { ExecutionLogs, IntentExecution, PlanningAttempt } from '../types'

type LogStream = keyof ExecutionLogs
type LoadLogs = (projectId: string, executionId: string) => Promise<ExecutionLogs>

function durationLabel(record: IntentExecution | PlanningAttempt) {
  if (!record.started_at) return '—'
  const start = Date.parse(record.started_at)
  const finish = record.finished_at ? Date.parse(record.finished_at) : Date.now()
  if (!Number.isFinite(start) || !Number.isFinite(finish)) return '—'
  return `${Math.max(0, (finish - start) / 1000).toFixed(1)}s`
}

export function LogConsole({
  projectId,
  execution,
  planningAttempt = null,
  liveLogs,
  loadLogs = api.executionLogs,
}: {
  projectId: string
  execution: IntentExecution | null
  planningAttempt?: PlanningAttempt | null
  liveLogs: ExecutionLogs
  loadLogs?: LoadLogs
}) {
  const [stream, setStream] = useState<LogStream>('stdout')
  const [storedLogs, setStoredLogs] = useState<ExecutionLogs>({ stdout: '', stderr: '' })
  const [liveBaseline, setLiveBaseline] = useState({ stdout: 0, stderr: 0 })
  const [cleared, setCleared] = useState(false)
  const [follow, setFollow] = useState(true)
  const [collapsed, setCollapsed] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const viewportRef = useRef<HTMLPreElement>(null)
  const record = planningAttempt ?? execution

  useEffect(() => {
    setStoredLogs({ stdout: '', stderr: '' })
    setLiveBaseline({
      stdout: liveLogs.stdout.length,
      stderr: liveLogs.stderr.length,
    })
    setCleared(false)
    setError(null)
    if (!record) return
    let active = true
    const loader = planningAttempt ? api.planningLogs : loadLogs
    void loader(projectId, record.id)
      .then(logs => {
        if (!active) return
        setStoredLogs(logs)
        setLiveBaseline(current => ({
          stdout: logs.stdout ? current.stdout : 0,
          stderr: logs.stderr ? current.stderr : 0,
        }))
      })
      .catch(reason => { if (active) setError(reason instanceof Error ? reason.message : String(reason)) })
    return () => { active = false }
  }, [execution?.id, loadLogs, planningAttempt?.id, projectId])

  const visibleText = cleared
    ? ''
    : capLogText(storedLogs[stream] + liveLogs[stream].slice(liveBaseline[stream]))

  useEffect(() => {
    if (follow && viewportRef.current) {
      viewportRef.current.scrollTop = viewportRef.current.scrollHeight
    }
  }, [follow, visibleText])

  return (
    <section className={collapsed ? 'log-console is-collapsed' : 'log-console'} aria-label="Agent 实时日志">
      <header className="log-header">
        <div className="log-identity">
          <strong>{record ? `${record.provider} · ${record.id}` : 'Audit logs'}</strong>
          {record && <span className={`log-status log-status--${record.status}`}>{record.status}</span>}
        </div>
        {record && (
          <div className="log-metrics">
            <span>{planningAttempt ? 'planning' : `exit ${execution?.exit_code ?? '—'}`}</span>
            <span>{durationLabel(record)}</span>
            <span className="work-dir" title={record.work_dir}>{record.work_dir}</span>
          </div>
        )}
        <div className="log-actions">
          <label className="follow-toggle">
            <input type="checkbox" checked={follow} onChange={event => setFollow(event.target.checked)} />
            <span>跟随</span>
          </label>
          <button className="icon-button" aria-label="清空当前视图" title="清空当前视图" disabled={!record} onClick={() => setCleared(true)}><Trash2 size={16} /></button>
          <button className="icon-button" aria-label={collapsed ? '展开日志' : '折叠日志'} title={collapsed ? '展开日志' : '折叠日志'} onClick={() => setCollapsed(value => !value)}>
            {collapsed ? <ChevronUp size={17} /> : <ChevronDown size={17} />}
          </button>
        </div>
      </header>
      {!collapsed && (
        <>
          <div className="log-tabs" role="tablist" aria-label="日志流">
            {(['stdout', 'stderr'] as const).map(value => (
              <button
                key={value}
                role="tab"
                aria-selected={stream === value}
                className={stream === value ? 'log-tab is-active' : 'log-tab'}
                onClick={() => { setStream(value); setCleared(false) }}
              >
                {value}
              </button>
            ))}
          </div>
          <pre ref={viewportRef} className="log-output">{error ?? visibleText}</pre>
        </>
      )}
    </section>
  )
}
