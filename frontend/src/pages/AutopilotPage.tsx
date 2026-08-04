import { ArrowLeft, Bot, Eye, EyeOff, ExternalLink, Square } from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'

import { api } from '../api/client'
import type { AutoSession, AutopilotSnapshot } from '../types'

const statusLabel: Record<string, string> = {
  starting: '启动中',
  running: '运行中',
  waiting: '等待平台',
  submitting: '提交答案',
  stopping: '停止中',
  finished: '已完成',
  failed: '失败',
  stopped: '已停止',
}

export function AutopilotPage({ onBack, onOpenProject }: { onBack: () => void; onOpenProject: (id: string) => void }) {
  const [snapshot, setSnapshot] = useState<AutopilotSnapshot | null>(null)
  const [prompt, setPrompt] = useState('')
  const [showPrompt, setShowPrompt] = useState(false)
  const [controller, setController] = useState('')
  const [solvers, setSolvers] = useState<string[]>([])
  const [agentLimit, setAgentLimit] = useState(2)
  const [maxTasks, setMaxTasks] = useState(10)
  const [timeoutMinutes, setTimeoutMinutes] = useState(60)
  const [controllerTimeout, setControllerTimeout] = useState(300)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function refresh() {
    try {
      const next = await api.getAutopilot()
      setSnapshot(next)
      setController(current => current || next.providers[0] || '')
      setSolvers(current => current.length > 0 ? current : next.providers.slice(0, 2))
      setError(null)
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught))
    }
  }

  useEffect(() => {
    void refresh()
    const timer = window.setInterval(() => { void refresh() }, 2000)
    return () => window.clearInterval(timer)
  }, [])

  const session = snapshot?.session ?? null
  const active = session !== null && !['finished', 'failed', 'stopped'].includes(session.status)
  const available = snapshot?.providers ?? []
  const selectedLimit = Math.max(1, Math.min(agentLimit, Math.max(1, solvers.length)))
  const canStart = prompt.trim().length > 0 && controller.length > 0 && solvers.length > 0 && !active && !busy

  const history = useMemo(() => session?.history ?? [], [session])

  function toggleSolver(provider: string) {
    setSolvers(current => current.includes(provider) ? current.filter(item => item !== provider) : [...current, provider])
  }

  async function start() {
    if (!canStart) return
    try {
      setBusy(true)
      setError(null)
      const next = await api.startAutopilot({
        platform_prompt: prompt,
        controller_provider: controller,
        solver_providers: solvers,
        agent_limit: selectedLimit,
        max_tasks: maxTasks,
        task_timeout_minutes: timeoutMinutes,
        controller_timeout_seconds: controllerTimeout,
      })
      setSnapshot(next)
      setShowPrompt(false)
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught))
    } finally {
      setBusy(false)
    }
  }

  async function stop() {
    if (busy) return
    try {
      setBusy(true)
      setError(null)
      setSnapshot(await api.stopAutopilot())
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught))
    } finally {
      setBusy(false)
    }
  }

  return (
    <main className="auto-shell">
      <header className="auto-header">
        <button className="icon-button" aria-label="返回项目" title="返回项目" onClick={onBack}><ArrowLeft size={20} /></button>
        <div>
          <div className="auto-kicker"><Bot size={15} /> AUTOMATIC MODE</div>
          <h1>全自动答题</h1>
        </div>
        {session && <span className={`auto-status auto-status--${session.status}`}>{statusLabel[session.status] ?? session.status}</span>}
      </header>

      {error && <p className="form-error auto-error" role="alert">{error}</p>}

      <div className="auto-layout">
        <section className="auto-panel auto-config-panel">
          <div className="auto-panel-heading"><div><span className="auto-panel-kicker">PLATFORM</span><h2>连接配置</h2></div></div>
          <label className="auto-field auto-field--full">
            <span>平台 API prompt</span>
            <div className="prompt-editor">
              {showPrompt ? <textarea value={prompt} onChange={event => setPrompt(event.target.value)} placeholder="粘贴平台 API、token 和答题接口说明" rows={10} /> : <div className="prompt-hidden">{prompt ? '已输入平台 prompt，点击右侧按钮查看' : '尚未输入平台 prompt'}</div>}
              <button className="icon-button prompt-toggle" type="button" aria-label={showPrompt ? '隐藏平台 prompt' : '显示平台 prompt'} title={showPrompt ? '隐藏 prompt' : '显示 prompt'} onClick={() => setShowPrompt(value => !value)}>{showPrompt ? <EyeOff size={17} /> : <Eye size={17} />}</button>
            </div>
          </label>
          <div className="auto-fields-grid">
            <label className="auto-field"><span>控制器 provider</span><select value={controller} onChange={event => setController(event.target.value)} disabled={active}>{available.map(provider => <option key={provider} value={provider}>{provider}</option>)}</select></label>
            <label className="auto-field"><span>每题超时（分钟）</span><input type="number" min={1} max={1440} value={timeoutMinutes} onChange={event => setTimeoutMinutes(Number(event.target.value))} disabled={active} /></label>
            <label className="auto-field"><span>最多题数</span><input type="number" min={1} max={1000} value={maxTasks} onChange={event => setMaxTasks(Number(event.target.value))} disabled={active} /></label>
            <label className="auto-field"><span>控制器超时（秒）</span><input type="number" min={1} max={3600} value={controllerTimeout} onChange={event => setControllerTimeout(Number(event.target.value))} disabled={active} /></label>
          </div>
          <fieldset className="auto-provider-choices"><legend>解题 provider</legend>{available.map(provider => <label key={provider} className="choice-row"><input type="checkbox" checked={solvers.includes(provider)} onChange={() => toggleSolver(provider)} disabled={active} /><span>{provider}</span></label>)}</fieldset>
          <label className="auto-field"><span>并行 Agent 数量</span><input type="number" min={1} max={Math.max(1, solvers.length)} value={selectedLimit} onChange={event => setAgentLimit(Number(event.target.value))} disabled={active || solvers.length === 0} /></label>
          <div className="auto-actions"><button className="primary-button" onClick={() => void start()} disabled={!canStart}>{busy ? '处理中...' : '启动全自动'}</button><button className="danger-button" onClick={() => void stop()} disabled={!active || busy}><Square size={16} /> 停止</button></div>
        </section>

        <section className="auto-panel auto-progress-panel">
          <div className="auto-panel-heading"><div><span className="auto-panel-kicker">SESSION</span><h2>运行进度</h2></div>{session && <span className="auto-count">{session.completed_tasks} / {session.max_tasks}</span>}</div>
          {!session && <p className="empty-state">还没有自动会话。</p>}
          {session && <>
            <dl className="auto-summary"><div><dt>当前题目</dt><dd>{session.current_task_name ?? '等待下一题'}</dd></div><div><dt>外部 ID</dt><dd>{session.current_external_id ?? '未分配'}</dd></div><div><dt>黑板项目</dt><dd>{session.current_project_id ? <button className="text-button" onClick={() => onOpenProject(session.current_project_id!)}>{session.current_project_id}<ExternalLink size={14} /></button> : '未创建'}</dd></div></dl>
            {session.last_message && <p className="auto-message">{session.last_message}</p>}
            {session.last_error && <p className="form-error">{session.last_error}</p>}
            <div className="auto-history"><h3>已通过题目</h3>{history.length === 0 ? <p className="empty-state">暂无记录。</p> : <ol>{history.map(item => <li key={item.external_id}><div><strong>{item.name}</strong><small>{item.external_id}</small></div><button className="text-button" onClick={() => onOpenProject(item.project_id)}>{item.project_id}<ExternalLink size={13} /></button></li>)}</ol>}</div>
          </>}
        </section>
      </div>
    </main>
  )
}
