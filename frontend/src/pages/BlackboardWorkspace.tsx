import { Activity, AlertTriangle, ArrowLeft, Download, Eye, Hash, Pencil, Play, RotateCcw, Square, X } from 'lucide-react'
import { useEffect, useReducer, useRef, useState } from 'react'

import { api } from '../api/client'
import { connectProjectSocket, type ProjectSocketStatus } from '../api/projectSocket'
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
import { createProjectState, reduceProjectEvent, replaceRecordLogs, replaceSnapshot } from '../state/projectStore'
import type { GraphTool, IntentExecution, PlanningAttempt, WorkspaceMode } from '../types'
import { sumFactTokens } from '../utils/tokenEstimate'

export function BlackboardWorkspace({ projectId, onBack }: { projectId: string; onBack: () => void }) {
  const [state, dispatch] = useReducer((current: ReturnType<typeof createProjectState>, action: { type: 'snapshot'; value: any } | { type: 'event'; value: any } | { type: 'logs'; id: string; value: any }) => action.type === 'snapshot' ? replaceSnapshot(current, action.value) : action.type === 'logs' ? replaceRecordLogs(current, action.id, action.value.logs, action.value.offsets) : reduceProjectEvent(current, action.value), createProjectState())
  const [mode, setMode] = useState<WorkspaceMode>('browse')
  const [activeTool, setActiveTool] = useState<GraphTool>('select')
  const [connectSource, setConnectSource] = useState<string | null>(null)
  const [selection, setSelection] = useState<InspectorSelection>(null)
  const [selectedExecution, setSelectedExecution] = useState<IntentExecution | null>(null)
  const [selectedPlanning, setSelectedPlanning] = useState<PlanningAttempt | null>(null)
  const [panelMode, setPanelMode] = useState<'details' | 'diagnostics'>('details')
  const [recoveryDismissed, setRecoveryDismissed] = useState(false)
  const [actionError, setActionError] = useState<string | null>(null)
  const [showArchived, setShowArchived] = useState(false)
  const [flow, setFlow] = useState<BlackboardFlowInstance | null>(null)
  const [socketStatus, setSocketStatus] = useState<ProjectSocketStatus>('connecting')
  const previousNodeCount = useRef(0)
  const syncingLogs = useRef(new Set<string>())
  const snapshot = state.snapshot

  async function refresh() {
    dispatch({ type: 'snapshot', value: await api.getGraph(projectId, true) })
  }

  useEffect(() => { void refresh() }, [projectId])
  useEffect(() => {
    if (!state.snapshot) return
    const socket = connectProjectSocket(projectId, state.sequence, event => dispatch({ type: 'event', value: event }), setSocketStatus)
    return () => socket.close()
  }, [projectId, state.snapshot?.project.id])
  useEffect(() => { if (state.needsRefresh) void refresh() }, [state.needsRefresh])
  useEffect(() => {
    if (!snapshot) return
    const planningIds = new Set(snapshot.planning_attempts.map(attempt => attempt.id))
    for (const recordId of Object.keys(state.logSyncNeeded)) {
      if (syncingLogs.current.has(recordId)) continue
      const isPlanning = planningIds.has(recordId)
      const isExecution = snapshot.edges.some(edge =>
        (edge.executions ?? []).some(execution => execution.id === recordId),
      )
      if (!isPlanning && !isExecution) continue
      syncingLogs.current.add(recordId)
      const loader = isPlanning ? api.syncPlanningLogs : api.syncExecutionLogs
      void loader(projectId, recordId)
        .then(value => dispatch({ type: 'logs', id: recordId, value }))
        .catch(caught => setActionError(caught instanceof Error ? caught.message : String(caught)))
        .finally(() => syncingLogs.current.delete(recordId))
    }
  }, [projectId, snapshot?.sequence, state.logSyncNeeded])
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
  useEffect(() => {
    function handleKeyDown(event: KeyboardEvent) {
      if (mode !== 'edit' || !selection || (event.key !== 'Delete' && event.key !== 'Backspace')) return
      const target = event.target as HTMLElement | null
      if (target?.closest('input, textarea, select, [contenteditable="true"], [role="dialog"]')) return
      event.preventDefault()
      void removeSelection(selection)
    }
    window.addEventListener('keydown', handleKeyDown)
    return () => window.removeEventListener('keydown', handleKeyDown)
  }, [mode, projectId, selection, showArchived])

  if (!snapshot) return <main className="workspace-loading">正在加载黑板...</main>
  const currentSnapshot = snapshot
  const factTokenTotal = sumFactTokens(snapshot.nodes)

  async function saveNodePosition(nodeId: string, position: { x: number; y: number }) {
    if (mode !== 'edit') return
    const next = { ...currentSnapshot.layout, nodes: { ...currentSnapshot.layout.nodes, [nodeId]: position } }
    const layout = await api.saveLayout(projectId, next)
    dispatch({ type: 'snapshot', value: { ...currentSnapshot, layout } })
  }

  async function reconnect(command: ReconnectCommand) {
    if (mode !== 'edit') return
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
    if (mode !== 'edit') return
    const content = window.prompt('Fact 内容')?.trim()
    if (!content) return
    await api.addNode(projectId, { type: 'fact', content })
    await refresh()
  }

  async function archiveNode(nodeId: string) {
    if (mode !== 'edit') return
    if (!window.confirm('归档此节点？关联边会从活动图中隐藏，可通过“显示已归档”恢复。')) return
    await api.archiveNode(projectId, nodeId)
    setSelection(null)
    setSelectedExecution(null)
    await refresh()
  }

  async function restoreNode(nodeId: string) {
    if (mode !== 'edit') return
    await api.restoreNode(projectId, nodeId)
    setSelection(null)
    await refresh()
  }

  async function deleteEdge(edgeId: string) {
    if (mode !== 'edit') return
    const edge = currentSnapshot.edges.find(item => item.id === edgeId)
    if (!edge || (edge.executions?.length ?? 0) > 0) return
    if (!window.confirm('永久删除这条边？此操作不会删除两端节点。')) return
    await api.deleteEdge(projectId, edgeId)
    setSelection(null)
    setSelectedExecution(null)
    await refresh()
  }

  async function removeSelection(target: InspectorSelection) {
    try {
      setActionError(null)
      if (target?.kind === 'node') {
        if (target.value.state === 'archived') await restoreNode(target.value.id)
        else await archiveNode(target.value.id)
      } else if (target?.kind === 'edge') {
        await deleteEdge(target.value.id)
      }
    } catch (caught) {
      setActionError(caught instanceof Error ? caught.message : String(caught))
      await refresh()
    }
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
  const removeKind = selection?.kind === 'node'
    ? selection.value.state === 'archived'
      ? 'restore'
      : selection.value.sources.includes('human')
        && (selection.value.type === 'fact' || selection.value.type === 'hint')
        && !selection.value.produced_by
        ? 'archive'
        : null
    : selection?.kind === 'edge' && (selection.value.executions?.length ?? 0) === 0
      ? 'delete'
      : null

  return (
    <main className={showRecovery ? 'workspace-shell has-recovery' : 'workspace-shell'}>
      {actionError && <div className="error-banner">{actionError}</div>}
      <header className="workspace-header">
        <button className="icon-button" aria-label="返回项目总览" onClick={onBack}><ArrowLeft size={20} /></button>
        <div className="workspace-title"><strong>{snapshot.project.name}</strong><span className={`status status--${snapshot.project.status}`}>{snapshot.project.status}</span></div>
        <div className="generation-chip">最新一轮 · G{snapshot.highest_generation}</div>
        <div className="token-chip" title="当前项目所有 Fact 内容的估算 token 总量"><Hash size={14} /><span className="token-chip__label">Fact tokens ·</span><span>~{factTokenTotal}</span></div>
        <div className="workspace-actions">
          <button className={mode === 'browse' ? 'mode-button is-active' : 'mode-button'} onClick={() => changeMode('browse')}><Eye size={16} /> 浏览</button>
          <button className={mode === 'edit' ? 'mode-button is-active' : 'mode-button'} onClick={() => changeMode('edit')}><Pencil size={16} /> 编辑</button>
          <label className="archive-toggle" title="在画布中显示已归档节点及其关联边">
            <input type="checkbox" checked={showArchived} onChange={event => { setSelection(null); setShowArchived(event.target.checked) }} />
            显示已归档
          </label>
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
        removeKind={removeKind}
        onRemove={() => void removeSelection(selection)}
        onFit={() => flow?.fitView({ padding: .18 })}
      />
      <BlackboardGraph
        nodes={snapshot.nodes}
        edges={snapshot.edges}
        layout={snapshot.layout}
        highestGeneration={snapshot.highest_generation}
        mode={mode}
        connectingNodeId={connectSource}
        showArchived={showArchived}
        onSelect={handleGraphSelection}
        onLayoutChange={(id, position) => void saveNodePosition(id, position)}
        onReconnectIntent={command => void runAction(() => reconnect(command))}
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
          mode={mode}
          onArchiveNode={nodeId => void removeSelection({ kind: 'node', value: currentSnapshot.nodes.find(node => node.id === nodeId)! })}
          onRestoreNode={nodeId => void removeSelection({ kind: 'node', value: currentSnapshot.nodes.find(node => node.id === nodeId)! })}
          onDeleteEdge={edgeId => void removeSelection({ kind: 'edge', value: currentSnapshot.edges.find(edge => edge.id === edgeId)! })}
        />
      )}
      <LogConsole
        projectId={projectId}
        execution={selectedExecution}
        planningAttempt={selectedPlanning}
        liveLogs={(selectedPlanning || selectedExecution) ? state.logs[(selectedPlanning ?? selectedExecution)!.id] ?? { stdout: '', stderr: '' } : { stdout: '', stderr: '' }}
        socketStatus={socketStatus}
        streamingSupported={currentSnapshot.provider_health.find(
          health => health.provider === (selectedPlanning ?? selectedExecution)?.provider,
        )?.streaming_supported}
      />
    </main>
  )
}
