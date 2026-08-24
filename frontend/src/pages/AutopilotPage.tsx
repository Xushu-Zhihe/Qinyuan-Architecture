import { ArrowLeft, Bot, Check, Clock3, ExternalLink, FileJson2, MessageSquareText, RefreshCw, Save, ScanSearch, SkipForward, Square, Trash2 } from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'

import { api } from '../api/client'
import { AutopilotControllerFields } from '../components/AutopilotControllerFields'
import { StructuredPlatformForm } from '../components/StructuredPlatformForm'
import type { AutopilotSnapshot, AutopilotStartPayload, BlackboardNode, ControllerInput, CurrentTaskResult, PlatformValidationPayload, StructuredPlatformInput, StructuredPlatformTemplate, ValidationResult } from '../types'

const statusLabel: Record<string, string> = {
  starting: '启动中', queued: '等待取题', fetched: '已取题', solving: '解题中', solver_achieved: '已生成结果',
  submit_pending: '准备提交', replanning: '重新规划', agent_correcting: 'Agent 纠正中', retry_wait: '等待重试', waiting: '等待平台', awaiting_manual_review: '等待人工处理',
  accepted: '已接受', rejected: '已拒绝', skipped: '已跳过', stopping: '停止中', finished: '已完成', failed: '失败', stopped: '已停止',
}

const defaultController: ControllerInput = {
  api_key: '', base_url: 'https://api.openai.com/v1', model: '',
  advanced: { timeout_seconds: 300, max_rounds: 20, max_transport_retries: 2, temperature: null, max_output_tokens: null, reasoning_effort: null, extra_headers: {}, extra_body: {} },
}

const endpoint = (method: 'GET' | 'POST', path: string, body: unknown = null) => ({ method, path, query: {}, headers: {}, body, form: null, timeout_seconds: 30, max_retries: 0, retry_status_codes: [429, 500, 502, 503, 504] })
const defaultStructured: StructuredPlatformInput = {
  base_url: '',
  auth: { type: 'bearer', token: '', header_name: 'Authorization', prefix: 'Bearer', parameter_name: 'token' },
  default_headers: { Accept: 'application/json' },
  fetch_task: endpoint('GET', '/tasks/next'), current_task: null, task_info: null,
  submit_result: endpoint('POST', '/tasks/{{external_id}}/submit', { result: '{{submission.value}}' }), skip_task: null,
  mapping: { task_list_path: null, external_id_path: 'id', name_path: 'name', goal_path: 'goal', origin_path: 'origin', origin_endpoint_path: null, goal_format: 'plain', flag_header: null, hints_path: 'hints', hints_item_path: null, attachments_path: null, cursor_path: null, lease_id_path: null },
  outcomes: { fetch: { finished: null, empty: null }, submit: { accepted: null, rejected: null, retryable: { status_codes: [429, 500, 502, 503, 504] } }, skip: { succeeded: null } },
  health_check: null, task_info_mode: 'always', task_info_failure_action: 'fail_task', task_info_delay_seconds: 0, initial_cursor: null,
}

function formatDuration(seconds: number | null | undefined) {
  if (seconds == null || !Number.isFinite(seconds)) return '未计时'
  const whole = Math.max(0, Math.ceil(seconds))
  const minutes = Math.floor(whole / 60)
  const remainder = whole % 60
  return `${minutes}分 ${String(remainder).padStart(2, '0')}秒`
}

export function AutopilotPage({ onBack, onOpenProject }: { onBack: () => void; onOpenProject: (id: string) => void }) {
  const [snapshot, setSnapshot] = useState<AutopilotSnapshot | null>(null)
  const [mode, setMode] = useState<'prompt' | 'structured'>('prompt')
  const [controller, setController] = useState(defaultController)
  const [prompt, setPrompt] = useState('')
  const [structured, setStructured] = useState(defaultStructured)
  const [templates, setTemplates] = useState<StructuredPlatformTemplate[]>([])
  const [selectedTemplateId, setSelectedTemplateId] = useState('')
  const [templateName, setTemplateName] = useState('')
  const [sampleResponse, setSampleResponse] = useState('')
  const [solvers, setSolvers] = useState<string[]>([])
  const [agentLimit, setAgentLimit] = useState(1)
  const [maxTasks, setMaxTasks] = useState(10)
  const [timeoutMinutes, setTimeoutMinutes] = useState(60)
  const [rejectedAction, setRejectedAction] = useState<'pause' | 'replan' | 'agent_correct' | 'skip' | 'fail'>('agent_correct')
  const [solverFailureAction, setSolverFailureAction] = useState<'skip' | 'fail'>('fail')
  const [stopActiveTaskAction, setStopActiveTaskAction] = useState<'leave' | 'skip'>('leave')
  const [maxReplans, setMaxReplans] = useState(1)
  const [maxAgentCorrections, setMaxAgentCorrections] = useState(2)
  const [maxTransportRetries, setMaxTransportRetries] = useState(2)
  const [extensionSeconds, setExtensionSeconds] = useState(600)
  const [clockTick, setClockTick] = useState(() => Date.now())
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [controllerValidation, setControllerValidation] = useState<ValidationResult | null>(null)
  const [platformValidation, setPlatformValidation] = useState<ValidationResult | null>(null)
  const [currentTaskResult, setCurrentTaskResult] = useState<CurrentTaskResult | null>(null)
  const [reviewFacts, setReviewFacts] = useState<BlackboardNode[]>([])
  const [selectedFactId, setSelectedFactId] = useState('')

  async function refresh() {
    try {
      const next = await api.getAutopilot()
      setSnapshot(next)
      const ready = next.providers.filter(provider => next.provider_capabilities?.[provider]?.auto_ready ?? true)
      setSolvers(current => current.length > 0 ? current : ready.slice(0, 2))
      setError(null)
    } catch (caught) { setError(caught instanceof Error ? caught.message : String(caught)) }
  }

  async function refreshTemplates() {
    try { setTemplates(await api.listAutopilotTemplates()) }
    catch (caught) { setError(caught instanceof Error ? caught.message : String(caught)) }
  }

  useEffect(() => {
    void refresh()
    void refreshTemplates()
    const timer = window.setInterval(() => { void refresh() }, 2000)
    return () => { window.clearInterval(timer); setController(current => ({ ...current, api_key: '' })); setStructured(current => ({ ...current, auth: { ...current.auth, token: '' } })); setPrompt('') }
  }, [])

  useEffect(() => {
    const timer = window.setInterval(() => setClockTick(Date.now()), 1000)
    return () => window.clearInterval(timer)
  }, [])

  const session = snapshot?.session ?? null
  const active = session !== null && !['finished', 'failed', 'stopped'].includes(session.status)
  const available = snapshot?.providers ?? []
  const selectedLimit = Math.max(1, Math.min(agentLimit, Math.max(1, solvers.length)))
  const platformReady = mode === 'prompt' ? prompt.trim().length > 0 : structured.base_url.trim().length > 0 && structured.fetch_task.path.length > 0 && structured.submit_result.path.length > 0
  const canStart = platformReady && controller.api_key.length > 0 && controller.base_url.length > 0 && controller.model.length > 0 && solvers.length > 0 && !active && !busy
  const history = useMemo(() => session?.history ?? [], [session])
  const remainingSeconds = useMemo(() => {
    if (!session) return null
    if (session.current_task_deadline_at) {
      const deadline = Date.parse(session.current_task_deadline_at)
      if (Number.isFinite(deadline)) return Math.max(0, (deadline - clockTick) / 1000)
    }
    return session.current_task_remaining_seconds ?? null
  }, [session, clockTick])
  const taskControlReady = Boolean(active && session?.current_external_id && session.current_project_id && !session.skip_requested)
  const skipReady = taskControlReady && (mode === 'prompt' || structured.skip_task !== null)
  const awaitingManualReview = session?.current_task_status === 'awaiting_manual_review'

  async function loadReviewFacts(projectId = session?.current_project_id ?? null) {
    if (!projectId) return
    try {
      const graph = await api.getGraph(projectId)
      const facts = graph.nodes.filter(node => node.type === 'fact' && node.state === 'active')
      setReviewFacts(facts)
      setSelectedFactId(current => facts.some(fact => fact.id === current) ? current : '')
      setError(null)
    } catch (caught) { setError(caught instanceof Error ? caught.message : String(caught)) }
  }

  useEffect(() => {
    if (awaitingManualReview && session?.current_project_id) {
      void loadReviewFacts(session.current_project_id)
      return
    }
    setReviewFacts([])
    setSelectedFactId('')
  }, [awaitingManualReview, session?.current_project_id])

  function toggleSolver(provider: string) { setSolvers(current => current.includes(provider) ? current.filter(item => item !== provider) : [...current, provider]) }

  function applyTemplate() {
    const template = templates.find(item => item.id === selectedTemplateId)
    if (!template) return
    setStructured(current => ({ ...template.platform, task_info_delay_seconds: template.platform.task_info_delay_seconds ?? 0, auth: { ...template.platform.auth, token: current.auth.token } }))
    setSampleResponse('')
    setPlatformValidation(null)
    setCurrentTaskResult(null)
    setError(null)
  }

  async function saveTemplate() {
    const name = templateName.trim()
    if (!name) { setError('请输入模板名称'); return }
    const currentToken = structured.auth.token
    try {
      setBusy(true)
      const platform = { ...structured, auth: { ...structured.auth, token: '' } }
      const saved = await api.saveAutopilotTemplate({ name, platform })
      setTemplates(current => [...current, saved])
      setSelectedTemplateId(saved.id)
      setTemplateName('')
      setError(null)
    } catch (caught) { setError(caught instanceof Error ? caught.message : String(caught)) }
    finally {
      setStructured(current => ({ ...current, auth: { ...current.auth, token: currentToken } }))
      setBusy(false)
    }
  }

  async function deleteTemplate() {
    const template = templates.find(item => item.id === selectedTemplateId)
    if (!template) return
    try {
      setBusy(true)
      await api.deleteAutopilotTemplate(template.id)
      setTemplates(current => current.filter(item => item.id !== template.id))
      setSelectedTemplateId('')
      setError(null)
    } catch (caught) { setError(caught instanceof Error ? caught.message : String(caught)) }
    finally { setBusy(false) }
  }

  async function testController() {
    try { setBusy(true); setControllerValidation(await api.testAutopilotController(controller)); setError(null) }
    catch (caught) { setControllerValidation(null); setError(caught instanceof Error ? caught.message : String(caught)) }
    finally { setBusy(false) }
  }

  async function validatePlatform() {
    try {
      setBusy(true)
      let structuredValidation: PlatformValidationPayload | null = null
      if (mode === 'structured') {
        structuredValidation = { mode, platform: structured }
        if (sampleResponse.trim()) {
          try {
            structuredValidation.sample_response = JSON.parse(sampleResponse)
          } catch {
            throw new Error('样例响应必须是有效 JSON')
          }
        }
      }
      setPlatformValidation(mode === 'prompt'
        ? await api.validateAutopilotPrompt(controller, prompt)
        : await api.validateAutopilotPlatform(structuredValidation!))
      setError(null)
    } catch (caught) { setPlatformValidation(null); setError(caught instanceof Error ? caught.message : String(caught)) }
    finally { setBusy(false) }
  }

  async function testPlatformHealth() {
    if (mode !== 'structured' || structured.health_check === null) return
    try {
      setBusy(true)
      setPlatformValidation(await api.testAutopilotPlatformHealth(structured))
      setError(null)
    } catch (caught) { setPlatformValidation(null); setError(caught instanceof Error ? caught.message : String(caught)) }
    finally { setBusy(false) }
  }

  async function inspectCurrentTask() {
    if (mode !== 'structured' || structured.current_task === null) return
    try {
      setBusy(true)
      setCurrentTaskResult(await api.getAutopilotCurrentTask(structured))
      setError(null)
    } catch (caught) {
      setCurrentTaskResult(null)
      setError(caught instanceof Error ? caught.message : String(caught))
    } finally {
      setBusy(false)
    }
  }

  async function start() {
    if (!canStart) return
    const common = {
      controller, solver_providers: solvers, agent_limit: selectedLimit, max_tasks: maxTasks,
      task_timeout_minutes: timeoutMinutes, poll_interval_seconds: 1, rejected_action: rejectedAction,
      solver_failure_action: solverFailureAction, stop_active_task_action: stopActiveTaskAction,
      max_replans_after_rejection: maxReplans, max_agent_corrections: maxAgentCorrections, max_transport_retries: maxTransportRetries,
    }
    const payload: AutopilotStartPayload = mode === 'prompt' ? { ...common, mode, platform: { prompt } } : { ...common, mode, platform: structured }
    try {
      setBusy(true); setError(null); setSnapshot(await api.startAutopilot(payload))
      setController(current => ({ ...current, api_key: '' }))
      setStructured(current => ({ ...current, auth: { ...current.auth, token: '' } }))
      setPrompt('')
    } catch (caught) { setError(caught instanceof Error ? caught.message : String(caught)) }
    finally { setBusy(false) }
  }

  async function stop() {
    if (busy) return
    try { setBusy(true); setError(null); setSnapshot(await api.stopAutopilot()); setController(current => ({ ...current, api_key: '' })); setStructured(current => ({ ...current, auth: { ...current.auth, token: '' } })); setPrompt('') }
    catch (caught) { setError(caught instanceof Error ? caught.message : String(caught)) }
    finally { setBusy(false) }
  }

  async function extendTime() {
    if (!taskControlReady || extensionSeconds <= 0 || busy) return
    try {
      setBusy(true)
      setError(null)
      setSnapshot(await api.extendAutopilotTime(extensionSeconds))
    } catch (caught) { setError(caught instanceof Error ? caught.message : String(caught)) }
    finally { setBusy(false) }
  }

  async function skipCurrentTask() {
    if (!taskControlReady || busy) return
    try {
      setBusy(true)
      setError(null)
      setSnapshot(await api.skipAutopilotTask())
    } catch (caught) { setError(caught instanceof Error ? caught.message : String(caught)) }
    finally { setBusy(false) }
  }

  async function finishAfterCurrentTask() {
    if (!taskControlReady || busy || session?.finish_after_current_task_requested) return
    try {
      setBusy(true)
      setError(null)
      setSnapshot(await api.finishAfterCurrentAutopilotTask())
    } catch (caught) { setError(caught instanceof Error ? caught.message : String(caught)) }
    finally { setBusy(false) }
  }

  async function submitSelectedFact(finishAfterAccept: boolean) {
    if (!awaitingManualReview || !selectedFactId || busy) return
    try {
      setBusy(true)
      setError(null)
      setSnapshot(await api.submitAutopilotTask(selectedFactId, finishAfterAccept))
      setSelectedFactId('')
    } catch (caught) { setError(caught instanceof Error ? caught.message : String(caught)) }
    finally { setBusy(false) }
  }

  const templateControls = <div className="platform-template-tools">
    <label className="auto-field platform-template-select"><span>结构化 API 模板</span><select aria-label="结构化 API 模板" value={selectedTemplateId} onChange={event => setSelectedTemplateId(event.target.value)} disabled={active || busy}><option value="">选择已保存模板</option>{templates.map(template => <option key={template.id} value={template.id}>{template.name}</option>)}</select></label>
    <div className="platform-template-actions"><button className="secondary-button" type="button" onClick={applyTemplate} disabled={active || busy || !selectedTemplateId}><Check size={15} /> 套用</button><button className="icon-button" type="button" aria-label="删除结构化 API 模板" title="删除模板" onClick={() => void deleteTemplate()} disabled={active || busy || !selectedTemplateId}><Trash2 size={16} /></button></div>
    <label className="auto-field platform-template-name"><span>新模板名称</span><input value={templateName} onChange={event => setTemplateName(event.target.value)} placeholder="例如 NSSCTF Arena" maxLength={120} disabled={active || busy} /></label>
    <button className="secondary-button platform-template-save" type="button" onClick={() => void saveTemplate()} disabled={active || busy || !templateName.trim()}><Save size={15} /> 保存模板</button>
    <p className="auto-help">模板会保存 endpoint、字段映射和结果判断，不保存平台 token。</p>
  </div>

  return <main className="auto-shell">
    <header className="auto-header"><button className="icon-button" aria-label="返回项目" title="返回项目" onClick={onBack}><ArrowLeft size={20} /></button><div><div className="auto-kicker"><Bot size={15} /> AUTOMATIC MODE</div><h1>全自动答题</h1></div>{session && <span className={`auto-status auto-status--${session.status}`}>{statusLabel[session.status] ?? session.status}</span>}</header>
    {error && <p className="form-error auto-error" role="alert">{error}</p>}
    <div className="auto-layout">
      <section className="auto-panel auto-config-panel">
        <div className="auto-panel-heading"><div><span className="auto-panel-kicker">CONNECTION</span><h2>连接配置</h2></div><div className="mode-segments" role="group" aria-label="平台接入模式"><button className={mode === 'prompt' ? 'is-active' : ''} onClick={() => setMode('prompt')} disabled={active}><MessageSquareText size={15} /> Prompt</button><button className={mode === 'structured' ? 'is-active' : ''} onClick={() => setMode('structured')} disabled={active}><FileJson2 size={15} /> 结构化 API</button></div></div>
        <AutopilotControllerFields value={controller} disabled={active || busy} validation={controllerValidation} onChange={value => { setController(value); setControllerValidation(null) }} onTest={() => void testController()} />
        {mode === 'structured' && <section className="auto-config-section">{templateControls}</section>}
        <section className="auto-config-section"><div className="auto-section-heading"><h3>平台</h3><div className="auto-inline-actions"><button className="secondary-button" type="button" onClick={() => void validatePlatform()} disabled={active || busy || !platformReady}>验证配置</button>{mode === 'structured' && structured.current_task !== null && <button className="secondary-button" type="button" onClick={() => void inspectCurrentTask()} disabled={active || busy || !structured.base_url.trim()}><ScanSearch size={15} /> 查看当前题目</button>}{mode === 'structured' && structured.health_check !== null && <button className="secondary-button" type="button" onClick={() => void testPlatformHealth()} disabled={active || busy || !structured.base_url.trim()}>测试健康接口</button>}</div></div>{mode === 'prompt' ? <label className="auto-field auto-field--full"><span>平台说明 prompt</span><textarea value={prompt} onChange={event => { setPrompt(event.target.value); setPlatformValidation(null) }} placeholder="平台协议说明：URL、认证、取题、提交、结果判断与放弃规则" rows={9} disabled={active} /></label> : <><StructuredPlatformForm value={structured} disabled={active} onChange={value => { setStructured(value); setPlatformValidation(null); setCurrentTaskResult(null) }} /><label className="auto-field auto-field--full"><span>样例响应 JSON（可选）</span><textarea value={sampleResponse} onChange={event => { setSampleResponse(event.target.value); setPlatformValidation(null) }} placeholder="粘贴不含 token 的 fetch 样例响应" rows={6} disabled={active} /></label></>}{platformValidation && <p className={`validation-state validation-state--${platformValidation.status}`}>{platformValidation.status === 'valid' ? (platformValidation.mapping_preview ? '配置与样例映射有效' : '配置有效') : [...(platformValidation.missing ?? []).map(item => `缺少：${item}`), ...(platformValidation.warnings ?? []), ...(platformValidation.mapping_preview?.errors ?? [])].join('；')}</p>}{currentTaskResult && (currentTaskResult.task ? <div className="current-task-preview" aria-live="polite"><div><strong>{currentTaskResult.task.name}</strong><small>{currentTaskResult.task.external_id}</small></div><dl><div><dt>Goal</dt><dd>{currentTaskResult.task.goal}</dd></div><div><dt>Origin</dt><dd>{currentTaskResult.task.origin}</dd></div>{currentTaskResult.task.hints.length > 0 && <div><dt>提示</dt><dd>{currentTaskResult.task.hints.join('；')}</dd></div>}</dl></div> : <p className="validation-state validation-state--warning" aria-live="polite">{currentTaskResult.message ?? '当前没有题目'}</p>)}</section>
        <section className="auto-config-section"><div className="auto-fields-grid"><label className="auto-field"><span>每题超时（分钟）</span><input type="number" min={1} max={1440} value={timeoutMinutes} onChange={event => setTimeoutMinutes(Number(event.target.value))} disabled={active} /></label><label className="auto-field"><span>最多题数</span><input type="number" min={1} max={1000} value={maxTasks} onChange={event => setMaxTasks(Number(event.target.value))} disabled={active} /></label><label className="auto-field"><span>拒答后动作</span><select value={rejectedAction} onChange={event => setRejectedAction(event.target.value as typeof rejectedAction)} disabled={active}><option value="agent_correct">退回 Agent 继续思考</option><option value="pause">暂停并人工处理</option><option value="fail">终止</option><option value="replan">重新规划提交</option><option value="skip">跳过</option></select></label><label className="auto-field"><span>Agent 纠正次数</span><input type="number" min={0} max={10} value={maxAgentCorrections} onChange={event => setMaxAgentCorrections(Number(event.target.value))} disabled={active} /></label><label className="auto-field"><span>业务重新规划次数</span><input type="number" min={0} max={10} value={maxReplans} onChange={event => setMaxReplans(Number(event.target.value))} disabled={active} /></label><label className="auto-field"><span>网络/限流传输重试</span><input type="number" min={0} max={10} value={maxTransportRetries} onChange={event => setMaxTransportRetries(Number(event.target.value))} disabled={active} /></label><label className="auto-field"><span>solver 失败动作</span><select value={solverFailureAction} onChange={event => setSolverFailureAction(event.target.value as typeof solverFailureAction)} disabled={active}><option value="fail">终止</option><option value="skip">跳过</option></select></label><label className="auto-field"><span>停止时 lease</span><select value={stopActiveTaskAction} onChange={event => setStopActiveTaskAction(event.target.value as typeof stopActiveTaskAction)} disabled={active}><option value="leave">保留</option><option value="skip">释放/跳过</option></select></label></div><p className="auto-help">Flag 被拒绝时默认保留附件并退回 Agent；达到纠正次数后进入人工处理。传输重试只处理网络/限流。</p><fieldset className="auto-provider-choices"><legend>Solver providers</legend>{available.map(provider => { const capability = snapshot?.provider_capabilities?.[provider]; const ready = capability?.auto_ready ?? true; return <label key={provider} className="choice-row"><input type="checkbox" checked={solvers.includes(provider)} onChange={() => toggleSolver(provider)} disabled={active || !ready} /><span>{provider}</span>{!ready && <small>{capability?.status ?? 'legacy-only'}</small>}</label> })}</fieldset><label className="auto-field"><span>每题 Agent 数量</span><input type="number" min={1} max={Math.max(1, solvers.length)} value={selectedLimit} onChange={event => setAgentLimit(Number(event.target.value))} disabled={active || solvers.length === 0} /></label></section>
        <div className="auto-actions"><button className="primary-button" onClick={() => void start()} disabled={!canStart}>{busy ? '处理中...' : '启动全自动'}</button><button className="danger-button" onClick={() => void stop()} disabled={!active || busy}><Square size={16} /> 停止</button></div>
      </section>
      <section className="auto-panel auto-progress-panel">
        <div className="auto-panel-heading"><div><span className="auto-panel-kicker">SESSION</span><h2>运行进度</h2></div>{session && <span className="auto-count">{session.completed_tasks} / {session.max_tasks}</span>}</div>
        {!session && <p className="empty-state">还没有自动会话。</p>}
        {session && <>
          <dl className="auto-summary"><div><dt>阶段</dt><dd>{statusLabel[session.current_task_status ?? session.status] ?? session.current_task_status ?? session.status}</dd></div><div><dt>当前题目</dt><dd>{session.current_task_name ?? '等待下一题'}</dd></div><div><dt>外部 ID</dt><dd>{session.current_external_id ?? '未分配'}</dd></div><div><dt>黑板项目</dt><dd>{session.current_project_id ? <button className="text-button" onClick={() => onOpenProject(session.current_project_id!)}>{session.current_project_id}<ExternalLink size={14} /></button> : '未创建'}</dd></div></dl>
          {taskControlReady && <div className="auto-task-controls">
            {!awaitingManualReview && <div className="auto-task-timer"><Clock3 size={16} /><span>本题剩余 {formatDuration(remainingSeconds)}</span></div>}
            <div className="auto-task-actions">
              {!awaitingManualReview && <><label className="auto-field"><span>增加秒数</span><input type="number" min={1} max={86400} step={60} value={extensionSeconds} onChange={event => setExtensionSeconds(Number(event.target.value))} disabled={busy} /></label><button className="secondary-button" type="button" onClick={() => void extendTime()} disabled={busy || extensionSeconds <= 0}><Clock3 size={15} /> 增加时间</button></>}
              <button className="secondary-button" type="button" onClick={() => void finishAfterCurrentTask()} disabled={busy || Boolean(session.finish_after_current_task_requested) || ['submit_pending', 'accepted', 'skipped', 'stopped'].includes(session.current_task_status ?? '')}><Check size={15} /> 本题完成后停止</button>
              <button className="danger-button" type="button" onClick={() => void skipCurrentTask()} disabled={!skipReady || busy || ['submit_pending', 'accepted', 'skipped', 'stopped'].includes(session.current_task_status ?? '')}><SkipForward size={15} /> 放弃本题</button>
            </div>
          </div>}
          {awaitingManualReview && <section className="auto-manual-review" aria-label="人工提交">
            <div className="auto-manual-review-heading"><h3>人工提交</h3><button className="icon-button" type="button" aria-label="刷新 Fact" title="刷新 Fact" onClick={() => void loadReviewFacts()} disabled={busy}><RefreshCw size={16} /></button></div>
            <label className="auto-field"><span>Fact</span><select aria-label="提交 Fact" value={selectedFactId} onChange={event => setSelectedFactId(event.target.value)} disabled={busy}><option value="">选择活动 Fact</option>{reviewFacts.map(fact => <option key={fact.id} value={fact.id}>{fact.id} · {fact.content.slice(0, 80)}</option>)}</select></label>
            <div className="auto-manual-review-actions"><button className="primary-button" type="button" onClick={() => void submitSelectedFact(false)} disabled={busy || !selectedFactId}><Check size={15} /> 提交并继续</button><button className="secondary-button" type="button" onClick={() => void submitSelectedFact(true)} disabled={busy || !selectedFactId}><Square size={15} /> 提交并结束</button></div>
          </section>}
          {session.current_goal_result && <div className="auto-result"><strong>结果 Fact</strong><span>{session.current_goal_result.fact_id ?? '未分配'}</span>{session.current_goal_result.preview && <p>{session.current_goal_result.preview}</p>}</div>}
          {snapshot?.controller_health && <p className="auto-message">Controller：{snapshot.controller_health.status}{snapshot.controller_health.model ? ` · ${snapshot.controller_health.model}` : ''}</p>}
          {session.last_message && <p className="auto-message">{session.last_message}</p>}
          {session.last_error && <p className="form-error">{session.last_error}</p>}
          <div className="auto-history"><h3>题目历史</h3>{history.length === 0 ? <p className="empty-state">暂无记录。</p> : <ol>{history.map((item, index) => <li key={`${item.external_id}-${index}`}><div><strong>{item.name}</strong><small>{statusLabel[item.status] ?? item.status} · {item.external_id}{item.reason_code ? ` · ${item.reason_code}` : ''}{item.attempt_count ? ` · ${item.attempt_count} 次提交` : ''}</small></div>{item.project_id && <button className="text-button" onClick={() => onOpenProject(item.project_id)}>{item.project_id}<ExternalLink size={13} /></button>}</li>)}</ol>}</div>
        </>}
      </section>
    </div>
  </main>
}
