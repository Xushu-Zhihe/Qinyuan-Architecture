import { useEffect, useMemo, useState, type Dispatch, type SetStateAction } from 'react'
import { ArrowLeft, Bot, ExternalLink, Play, Send, Square } from 'lucide-react'

import { api } from '../api/client'
import type {
  AutomationLLMInput,
  AutomationManifestDocument,
  AutomationRunSnapshot,
} from '../types'

const defaultLLM = (): AutomationLLMInput => ({
  base_url: 'https://llm.example/v1',
  api_key: '',
  model: 'model',
  timeout_seconds: 300,
  max_transport_retries: 2,
  temperature: null,
})

function increaseTimeout(value: number): number {
  return Math.min(3600, Math.max(1, value + 300))
}

export function AutomationPage({
  onBack,
  onOpenProject,
}: {
  onBack: () => void
  onOpenProject: (id: string) => void
}) {
  const [sourceText, setSourceText] = useState('')
  const [parserLLM, setParserLLM] = useState(defaultLLM)
  const [dispatcherLLM, setDispatcherLLM] = useState(defaultLLM)
  const [platformToken, setPlatformToken] = useState('')
  const [providers, setProviders] = useState('qinyuan-agent')
  const [agentLimit, setAgentLimit] = useState(1)
  const [maxTasks, setMaxTasks] = useState(10)
  const [taskTimeout, setTaskTimeout] = useState(3600)
  const [pollInterval, setPollInterval] = useState(5)
  const [manifest, setManifest] = useState<AutomationManifestDocument | null>(null)
  const [manifestJson, setManifestJson] = useState('')
  const [run, setRun] = useState<AutomationRunSnapshot | null>(null)
  const [instruction, setInstruction] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [validation, setValidation] = useState<string | null>(null)
  const [manifestRunnable, setManifestRunnable] = useState(false)

  const active = Boolean(run && ['starting', 'running'].includes(run.status))
  const providerNames = useMemo(
    () => providers.split(',').map(value => value.trim()).filter(Boolean),
    [providers],
  )

  async function refreshRun() {
    try {
      setRun(await api.getCurrentAutomationRun())
    } catch (caught) {
      if (caught instanceof Error && caught.message.includes('no automation run')) setRun(null)
    }
  }

  useEffect(() => {
    void refreshRun()
    const timer = window.setInterval(() => { void refreshRun() }, 2000)
    return () => window.clearInterval(timer)
  }, [])

  function showError(caught: unknown) {
    setError(caught instanceof Error ? caught.message : String(caught))
  }

  async function parse() {
    try {
      setBusy(true)
      setError(null)
      const result = await api.parseAutomationManifest({ source_text: sourceText, parser_llm: parserLLM })
      setManifest(result)
      setManifestJson(result.json)
      setManifestRunnable(false)
      setValidation(null)
    } catch (caught) {
      showError(caught)
    } finally {
      setBusy(false)
    }
  }

  async function saveManifest() {
    if (!manifest) return
    try {
      setBusy(true)
      setError(null)
      const result = await api.updateAutomationManifest(manifest.manifest_id, JSON.parse(manifestJson))
      setManifest(result)
      setManifestJson(result.json)
      setManifestRunnable(false)
      setValidation(null)
    } catch (caught) {
      showError(caught)
    } finally {
      setBusy(false)
    }
  }

  async function validateManifest() {
    if (!manifest) return
    try {
      const result = await api.validateAutomationManifest(manifest.manifest_id)
      setManifestRunnable(result.valid)
      setValidation(result.valid ? 'Manifest 可以启动调度器。' : result.errors.join('；'))
    } catch (caught) {
      showError(caught)
    }
  }

  async function start() {
    if (!manifest) return
    try {
      setBusy(true)
      setError(null)
      const result = await api.startAutomation({
        manifest_id: manifest.manifest_id,
        dispatcher_llm: dispatcherLLM,
        platform_token: platformToken,
        solver_providers: providerNames,
        agent_limit: agentLimit,
        max_tasks: maxTasks,
        task_timeout_seconds: taskTimeout,
        poll_interval_seconds: pollInterval,
      })
      setRun(result)
    } catch (caught) {
      showError(caught)
    } finally {
      setBusy(false)
    }
  }

  async function stop() {
    if (!run) return
    try {
      setRun(await api.stopAutomation(run.run_id))
    } catch (caught) {
      showError(caught)
    }
  }

  async function sendInstruction() {
    if (!run || !instruction.trim()) return
    try {
      setRun(await api.sendAutomationInstruction(run.run_id, instruction))
      setInstruction('')
    } catch (caught) {
      showError(caught)
    }
  }

  function updateLLM(
    setter: Dispatch<SetStateAction<AutomationLLMInput>>,
    field: keyof AutomationLLMInput,
    value: string | number | null,
  ) {
    setter(current => ({ ...current, [field]: value }))
  }

  return (
    <main className="auto-shell">
      <header className="auto-header">
        <button className="icon-button" aria-label="返回项目" onClick={onBack}><ArrowLeft size={20} /></button>
        <div><div className="auto-kicker"><Bot size={15} /> MANIFEST AUTOMATION</div><h1>全自动模式</h1></div>
        {run && <span className={`auto-status auto-status--${run.status}`}>{run.status}</span>}
      </header>
      {error && <p className="form-error auto-error" role="alert">{error}</p>}
      <div className="auto-layout">
        <section className="auto-panel auto-config-panel">
          <div className="auto-panel-heading"><div><span className="auto-panel-kicker">PARSER</span><h2>离线解析器</h2></div></div>
          <label className="auto-field auto-field--full"><span>比赛手册 / Skills</span><textarea rows={10} value={sourceText} onChange={event => setSourceText(event.target.value)} disabled={active} placeholder="人工粘贴平台手册或 Skills，解析器不会访问平台。" /></label>
          <div className="auto-fields-grid">
            <label className="auto-field auto-field--wide"><span>Parser Base URL</span><input value={parserLLM.base_url} onChange={event => updateLLM(setParserLLM, 'base_url', event.target.value)} disabled={active} /></label>
            <label className="auto-field"><span>模型</span><input value={parserLLM.model} onChange={event => updateLLM(setParserLLM, 'model', event.target.value)} disabled={active} /></label>
            <label className="auto-field auto-field--wide"><span>API Key</span><input type="password" autoComplete="off" value={parserLLM.api_key} onChange={event => updateLLM(setParserLLM, 'api_key', event.target.value)} disabled={active} /></label>
            <label className="auto-field"><span>超时（秒）</span><input type="number" min={1} max={3600} value={parserLLM.timeout_seconds} onChange={event => updateLLM(setParserLLM, 'timeout_seconds', Number(event.target.value))} disabled={active} /></label>
            <label className="auto-field"><span>传输重试次数</span><input type="number" min={0} max={10} value={parserLLM.max_transport_retries} onChange={event => updateLLM(setParserLLM, 'max_transport_retries', Number(event.target.value))} disabled={active} /></label>
            <button className="secondary-button" type="button" onClick={() => setParserLLM(current => ({ ...current, timeout_seconds: increaseTimeout(current.timeout_seconds) }))} disabled={active}>一键增加超时</button>
          </div>
          <button className="primary-button" type="button" onClick={() => void parse()} disabled={busy || active || !sourceText.trim()}>解析并生成 Manifest</button>
        </section>

        <section className="auto-panel auto-progress-panel">
          <div className="auto-panel-heading"><div><span className="auto-panel-kicker">MANIFEST</span><h2>接口契约</h2></div></div>
          {!manifest ? <p className="empty-state">尚未生成 Manifest。</p> : <>
            <p className="auto-message">ID：{manifest.manifest_id} · Hash：{manifest.manifest_hash}</p>
          <label className="auto-field auto-field--full"><span>manifest.json（可人工修改）</span><textarea rows={16} value={manifestJson} onChange={event => { setManifestJson(event.target.value); setManifestRunnable(false); setValidation(null) }} disabled={active} /></label>
            <pre className="auto-result">{manifest.markdown}</pre>
            <div className="auto-actions"><button className="secondary-button" type="button" onClick={() => void saveManifest()} disabled={busy || active}>保存人工修改</button><button className="secondary-button" type="button" onClick={() => void validateManifest()} disabled={busy || active}>校验执行条件</button></div>
            {validation && <p className="validation-state validation-state--warning">{validation}</p>}
          </>}
        </section>

        <section className="auto-panel auto-config-panel">
          <div className="auto-panel-heading"><div><span className="auto-panel-kicker">DISPATCHER</span><h2>调度器与工作组</h2></div></div>
          <div className="auto-fields-grid">
            <label className="auto-field auto-field--wide"><span>Dispatcher Base URL</span><input value={dispatcherLLM.base_url} onChange={event => updateLLM(setDispatcherLLM, 'base_url', event.target.value)} disabled={active} /></label>
            <label className="auto-field"><span>模型</span><input value={dispatcherLLM.model} onChange={event => updateLLM(setDispatcherLLM, 'model', event.target.value)} disabled={active} /></label>
            <label className="auto-field auto-field--wide"><span>API Key</span><input type="password" autoComplete="off" value={dispatcherLLM.api_key} onChange={event => updateLLM(setDispatcherLLM, 'api_key', event.target.value)} disabled={active} /></label>
            <label className="auto-field"><span>超时（秒）</span><input type="number" min={1} max={3600} value={dispatcherLLM.timeout_seconds} onChange={event => updateLLM(setDispatcherLLM, 'timeout_seconds', Number(event.target.value))} disabled={active} /></label>
            <label className="auto-field"><span>传输重试次数</span><input type="number" min={0} max={10} value={dispatcherLLM.max_transport_retries} onChange={event => updateLLM(setDispatcherLLM, 'max_transport_retries', Number(event.target.value))} disabled={active} /></label>
            <button className="secondary-button" type="button" onClick={() => setDispatcherLLM(current => ({ ...current, timeout_seconds: increaseTimeout(current.timeout_seconds) }))} disabled={active}>一键增加超时</button>
            <label className="auto-field auto-field--wide"><span>平台 Token</span><input type="password" autoComplete="off" value={platformToken} onChange={event => setPlatformToken(event.target.value)} disabled={active} /></label>
            <label className="auto-field auto-field--wide"><span>Solver providers（逗号分隔）</span><input value={providers} onChange={event => setProviders(event.target.value)} disabled={active} /></label>
            <label className="auto-field"><span>并行 Solver 数</span><input type="number" min={1} max={Math.max(1, providerNames.length)} value={agentLimit} onChange={event => setAgentLimit(Number(event.target.value))} disabled={active} /></label>
            <label className="auto-field"><span>最多题数</span><input type="number" min={1} max={1000} value={maxTasks} onChange={event => setMaxTasks(Number(event.target.value))} disabled={active} /></label>
            <label className="auto-field"><span>每题超时（秒）</span><input type="number" min={1} max={86400} value={taskTimeout} onChange={event => setTaskTimeout(Number(event.target.value))} disabled={active} /></label>
          </div>
          <div className="auto-actions"><button className="primary-button" type="button" onClick={() => void start()} disabled={busy || active || !manifestRunnable || !platformToken || providerNames.length === 0}><Play size={16} />启动调度</button><button className="danger-button" type="button" onClick={() => void stop()} disabled={!active}><Square size={16} />停止</button></div>
          {run && <div className="auto-summary"><div><dt>已处理题目</dt><dd>{run.processed_tasks} / {run.max_tasks}</dd></div><div><dt>黑板项目</dt><dd>{run.workgroup_project_id ? <button className="text-button" onClick={() => onOpenProject(run.workgroup_project_id!)}>{run.workgroup_project_id}<ExternalLink size={14} /></button> : '未创建'}</dd></div></div>}
        </section>

        <section className="auto-panel auto-progress-panel">
          <div className="auto-panel-heading"><div><span className="auto-panel-kicker">HUMAN OVERRIDE</span><h2>人工调度指令</h2></div></div>
          <label className="auto-field auto-field--full"><span>告诉调度器下一步</span><textarea rows={4} value={instruction} onChange={event => setInstruction(event.target.value)} disabled={!active} placeholder="例如：放弃当前题目；下一题不做了；重新获取当前题目状态。" /></label>
          <button className="secondary-button" type="button" onClick={() => void sendInstruction()} disabled={!active || !instruction.trim()}><Send size={15} />发送显式指令</button>
          <div className="auto-history"><h3>显式事件</h3>{!run || run.events.length === 0 ? <p className="empty-state">暂无事件。</p> : <ol>{run.events.slice().reverse().map(event => <li key={event.event_id}><div><strong>{event.event_type} · {event.status}</strong><small>{event.operation_id ?? event.command_id ?? event.event_id}</small></div><pre>{JSON.stringify(event.detail, null, 2)}</pre></li>)}</ol>}</div>
        </section>
      </div>
    </main>
  )
}
