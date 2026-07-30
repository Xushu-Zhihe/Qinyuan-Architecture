import { Activity, AlertTriangle, ArrowLeft, Download, Eye, Pencil, Play, RotateCcw, Square, X } from 'lucide-react'
import { useEffect, useReducer, useRef, useState } from 'react'

import { api } from '../api/client'
import { connectProjectSocket } from '../api/projectSocket'
import { GraphToolbar } from '../components/GraphToolbar'
import { DiagnosticsPanel } from '../components/DiagnosticsPanel'
import { InspectorPanel, type InspectorSelection } from '../components/InspectorPanel'
import { LogConsole } from '../components/LogConsole'
import {
  advanceClickConnection,
  BlackboardGraph,
  shouldFitAfterNodeChange,
  type BlackboardFlowInstance,
  type ReconnectCommand,
} from '../graph/BlackboardGraph'
import { createProjectState, reduceProjectEvent, replaceSnapshot } from '../state/projectStore'
import type { GraphTool, IntentExecution, PlanningAttempt, WorkspaceMode } from '../types'

export function BlackboardWorkspace({ projectId, onBack }: { projectId: string; onBack: () => void }) {
  const [state, dispatch] = useReducer((current: ReturnType<typeof createProjectState>, action: { type: 'snapshot'; value: any } | { type: 'event'; value: any }) => action.type === 'snapshot' ? replaceSnapshot(current, action.value) : reduceProjectEvent(current, action.value), createProjectState())
  const [mode, setMode] = useState<WorkspaceMode>('browse')
  const [activeTool, setActiveTool] = useState<GraphTool>('select')
  const [connectSource, setConnectSource] = useState<string | null>(null)
  const [selection, setSelection] = useState<InspectorSelection>(null)
  const [selectedExecution, setSelectedExecution] = useState<IntentExecution | null>(null)
  const [selectedPlanning, setSelectedPlanning] = useState<PlanningAttempt | null>(null)
  const [panelMode, setPanelMode] = useState<'details' | 'diagnostics'>('details')
  const [recoveryDismissed, setRecoveryDismissed] = useState(false)
  const [actionError, setActionError] = useState<string | null>(null)
  const [flow, setFlow] = useState<BlackboardFlowInstance | null>(null)
  const previousNodeCount = useRef(0)
  const snapshot = state.snapshot

  async function refresh() {
    dispatch({ type: 'snapshot', value: await api.getGraph(projectId) })
  }

  useEffect(() => { void refresh() }, [projectId])
  useEffect(() => {
    if (!state.snapshot) return
    const socket = connectProjectSocket(projectId, state.sequence, event => dispatch({ type: 'event', value: event }))
    return () => socket.close()
  }, [projectId, state.snapshot?.project.id])
  useEffect(() => { if (state.needsRefresh) void refresh() }, [state.needsRefresh])
  useEffect(() => {
    if (!flow || !snapshot) return
    const nextCount = snapshot.nodes.filter(node => node.state !== 'archived').length
    const shouldFit = shouldFitAfterNodeChange(previousNodeCount.current, nextCount)
    previousNodeCount.current = nextCount
    if (!shouldFit) return
    let timer = 0
    const frame = requestAnimationFrame(() => {
      timer = window.setTimeout(() => {
        void flow.fitView({ padding: .18, duration: 180 })
      }, 50)
    })
    return () => {
      cancelAnimationFrame(frame)
      window.clearTimeout(timer)
    }
  }, [flow, snapshot?.nodes.length])
  useEffect(() => {
    if (!snapshot || !selectedExecution) return
    const updated = snapshot.edges
      .flatMap(edge => edge.executions ?? [])
      .find(execution => execution.id === selectedExecution.id)
    if (updated) setSelectedExecution(updated)
  }, [snapshot?.sequence, selectedExecution?.id])
  useEffect(() => {
    if (!snapshot || !selectedPlanning) return
    const updated = snapshot.planning_attempts.find(
      attempt => attempt.id === selectedPlanning.id,
    )
    if (updated) setSelectedPlanning(updated)
  }, [snapshot?.sequence, selectedPlanning?.id])

  if (!snapshot) return <main className="workspace-loading">正在加载黑板...</main>
  const currentSnapshot = snapshot

  async function saveNodePosition(nodeId: string, position: { x: number; y: number }) {
    const next = { ...currentSnapshot.layout, nodes: { ...currentSnapshot.layout.nodes, [nodeId]: position } }
    const layout = await api.saveLayout(projectId, next)
    dispatch({ type: 'snapshot', value: { ...currentSnapshot, layout } })
  }

  async function reconnect(command: ReconnectCommand) {
    await api.updateEdge(projectId, command.edgeId, { from_node_id: command.source, to_node_id: command.target })
    await refresh()
  }

  async function connectNodes(source: string, target: string) {
    if (mode !== 'edit') return
    const content = window.prompt('Intent 内容')?.trim()
    if (!content) return
    await api.addEdge(projectId, { type: 'intent', from_node_id: source, to_node_id: target, content })
    await refresh()
  }

  function changeMode(nextMode: WorkspaceMode) {
    setMode(nextMode)
    if (nextMode === 'browse') {
      setActiveTool('select')
      setConnectSource(null)
    }
  }

  function handleGraphSelection(next: InspectorSelection) {
    setPanelMode('details')
    setSelectedPlanning(null)
    if (next?.kind === 'node') {
      const result = advanceClickConnection(activeTool, connectSource, next.value.id)
      if (activeTool === 'connect') {
        setSelection(next)
        setConnectSource(result.source)
        if (result.command) {
          setActiveTool('select')
          void connectNodes(result.command.source, result.command.target)
        }
        return
      }
    }
    setSelection(next)
  }

  async function addFact() {
    const content = window.prompt('Fact 内容')?.trim()
    if (!content) return
    await api.addNode(projectId, { type: 'fact', content })
    await refresh()
  }

  async function archiveNode(nodeId: string) {
    await api.archiveNode(projectId, nodeId)
    setSelection(null)
    setSelectedExecution(null)
    await refresh()
  }

  function selectExecution(execution: IntentExecution | null) {
    setSelectedExecution(execution)
    if (execution) setSelectedPlanning(null)
  }

  function selectPlanning(attempt: PlanningAttempt) {
    setSelectedPlanning(attempt)
    setSelectedExecution(null)
  }

  function selectIntent(intentId: string) {
    const edge = currentSnapshot.edges.find(item => item.id === intentId)
    if (!edge) return
    setSelection({ kind: 'edge', value: edge })
    setPanelMode('details')
    setSelectedPlanning(null)
  }

  async function runAction(action: () => Promise<unknown>) {
    try {
      setActionError(null)
      await action()
      await refresh()
    } catch (caught) {
      setActionError(caught instanceof Error ? caught.message : String(caught))
    }
  }

  async function retryExecution(execution: IntentExecution) {
    if (!window.confirm(`为 ${execution.id} 创建新的 Execution 重试？`)) return
    try {
      setActionError(null)
      const retried = await api.retryExecution(projectId, execution.id)
      setSelectedExecution(retried)
      setSelectedPlanning(null)
      await refresh()
    } catch (caught) {
      setActionError(caught instanceof Error ? caught.message : String(caught))
    }
  }

  async function reopenProject() {
    if (!window.confirm('重新打开项目会进入新的 Goal Version，旧答案仍保留在审计历史中。继续吗？')) return
    await runAction(() => api.reopenProject(projectId))
  }

  const showRecovery = currentSnapshot.recovery.total > 0 && !recoveryDismissed

  return (
    <main className={showRecovery ? 'workspace-shell has-recovery' : 'workspace-shell'}>
      {actionError && <div className="error-banner">{actionError}</div>}
      <header className="workspace-header">
        <button className="icon-button" aria-label="返回项目总览" onClick={onBack}><ArrowLeft size={20} /></button>
        <div className="workspace-title"><strong>{snapshot.project.name}</strong><span className={`status status--${snapshot.project.status}`}>{snapshot.project.status}</span></div>
        <div className="generation-chip">最新一轮 · G{snapshot.highest_generation}</div>
        <div className="workspace-actions">
          <button className={mode === 'browse' ? 'mode-button is-active' : 'mode-button'} onClick={() => changeMode('browse')}><Eye size={16} /> 浏览</button>
          <button className={mode === 'edit' ? 'mode-button is-active' : 'mode-button'} onClick={() => changeMode('edit')}><Pencil size={16} /> 编辑</button>
          <button className={panelMode === 'diagnostics' ? 'mode-button is-active' : 'mode-button'} onClick={() => setPanelMode(value => value === 'diagnostics' ? 'details' : 'diagnostics')}><Activity size={16} /> 诊断</button>
          <a className="icon-button" aria-label="导出审计包" title="导出审计包" href={api.auditExportUrl(projectId)}><Download size={17} /></a>
          {snapshot.project.status === 'running'
            ? <button className="secondary-button" onClick={() => void runAction(() => api.stopProject(projectId))}><Square size={15} /> 停止</button>
            : snapshot.project.status === 'achieved'
              ? <button className="secondary-button" onClick={() => void reopenProject()}><RotateCcw size={15} /> 重新打开</button>
              : <button className="primary-button" onClick={() => void runAction(() => api.startProject(projectId))}><Play size={15} /> 启动 Agent</button>}
        </div>
      </header>
      {showRecovery && (
        <div className="recovery-banner" role="status">
          <AlertTriangle size={16} />
          <span>服务重启曾中断 {currentSnapshot.recovery.executions} 个 Execution、{currentSnapshot.recovery.planning_attempts} 个 Planning Attempt；记录已保留在诊断视图。</span>
          <button className="icon-button" aria-label="关闭恢复提示" onClick={() => setRecoveryDismissed(true)}><X size={15} /></button>
        </div>
      )}
      <GraphToolbar
        mode={mode}
        activeTool={activeTool}
        onSelect={() => { setActiveTool('select'); setConnectSource(null) }}
        onAddFact={() => void addFact()}
        onConnect={() => { changeMode('edit'); setActiveTool('connect') }}
        onFit={() => flow?.fitView({ padding: .18 })}
      />
      <BlackboardGraph
        nodes={snapshot.nodes}
        edges={snapshot.edges}
        layout={snapshot.layout}
        highestGeneration={snapshot.highest_generation}
        mode={mode}
        connectingNodeId={connectSource}
        onSelect={handleGraphSelection}
        onLayoutChange={(id, position) => void saveNodePosition(id, position)}
        onReconnectIntent={command => void reconnect(command)}
        onConnectNodes={(source, target) => void connectNodes(source, target)}
        onInstance={setFlow}
      />
      {panelMode === 'diagnostics' ? (
        <DiagnosticsPanel
          snapshot={snapshot}
          onSelectExecution={selectExecution}
          onSelectPlanning={selectPlanning}
          onSelectIntent={selectIntent}
          onRetry={execution => void retryExecution(execution)}
        />
      ) : (
        <InspectorPanel
          selection={selection}
          snapshot={snapshot}
          onSelectExecution={selectExecution}
          onArchiveNode={nodeId => void archiveNode(nodeId)}
        />
      )}
      <LogConsole
        projectId={projectId}
        execution={selectedExecution}
        planningAttempt={selectedPlanning}
        liveLogs={selectedExecution ? state.logs[selectedExecution.id] ?? { stdout: '', stderr: '' } : { stdout: '', stderr: '' }}
      />
    </main>
  )
}
