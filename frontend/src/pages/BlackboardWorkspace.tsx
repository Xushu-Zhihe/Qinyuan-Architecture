import { ArrowLeft, Eye, Pencil, Play, Square } from 'lucide-react'
import { useEffect, useReducer, useState } from 'react'

import { api } from '../api/client'
import { connectProjectSocket } from '../api/projectSocket'
import { GraphToolbar } from '../components/GraphToolbar'
import { InspectorPanel, type InspectorSelection } from '../components/InspectorPanel'
import { LogConsole } from '../components/LogConsole'
import {
  BlackboardGraph,
  type BlackboardFlowInstance,
  type ReconnectCommand,
} from '../graph/BlackboardGraph'
import { createProjectState, reduceProjectEvent, replaceSnapshot } from '../state/projectStore'
import type { IntentExecution, WorkspaceMode } from '../types'

export function BlackboardWorkspace({ projectId, onBack }: { projectId: string; onBack: () => void }) {
  const [state, dispatch] = useReducer((current: ReturnType<typeof createProjectState>, action: { type: 'snapshot'; value: any } | { type: 'event'; value: any }) => action.type === 'snapshot' ? replaceSnapshot(current, action.value) : reduceProjectEvent(current, action.value), createProjectState())
  const [mode, setMode] = useState<WorkspaceMode>('browse')
  const [selection, setSelection] = useState<InspectorSelection>(null)
  const [selectedExecution, setSelectedExecution] = useState<IntentExecution | null>(null)
  const [flow, setFlow] = useState<BlackboardFlowInstance | null>(null)

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

  const snapshot = state.snapshot
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

  return (
    <main className="workspace-shell">
      <header className="workspace-header">
        <button className="icon-button" aria-label="返回项目总览" onClick={onBack}><ArrowLeft size={20} /></button>
        <div className="workspace-title"><strong>{snapshot.project.name}</strong><span className={`status status--${snapshot.project.status}`}>{snapshot.project.status}</span></div>
        <div className="generation-chip">最新一轮 · G{snapshot.highest_generation}</div>
        <div className="workspace-actions">
          <button className={mode === 'browse' ? 'mode-button is-active' : 'mode-button'} onClick={() => setMode('browse')}><Eye size={16} /> 浏览</button>
          <button className={mode === 'edit' ? 'mode-button is-active' : 'mode-button'} onClick={() => setMode('edit')}><Pencil size={16} /> 编辑</button>
          {snapshot.project.status === 'running' ? <button className="secondary-button" onClick={() => void api.stopProject(projectId).then(refresh)}><Square size={15} /> 停止</button> : <button className="primary-button" onClick={() => void api.startProject(projectId).then(refresh)}><Play size={15} /> 启动 Agent</button>}
        </div>
      </header>
      <GraphToolbar mode={mode} onAddFact={() => void addFact()} onConnect={() => setMode('edit')} onFit={() => flow?.fitView({ padding: .18 })} />
      <BlackboardGraph
        nodes={snapshot.nodes}
        edges={snapshot.edges}
        layout={snapshot.layout}
        highestGeneration={snapshot.highest_generation}
        mode={mode}
        onSelect={setSelection}
        onLayoutChange={(id, position) => void saveNodePosition(id, position)}
        onReconnectIntent={command => void reconnect(command)}
        onConnectNodes={(source, target) => void connectNodes(source, target)}
        onInstance={setFlow}
      />
      <InspectorPanel
        selection={selection}
        snapshot={snapshot}
        onSelectExecution={setSelectedExecution}
        onArchiveNode={nodeId => void archiveNode(nodeId)}
      />
      <LogConsole
        projectId={projectId}
        execution={selectedExecution}
        liveLogs={selectedExecution ? state.logs[selectedExecution.id] ?? { stdout: '', stderr: '' } : { stdout: '', stderr: '' }}
      />
    </main>
  )
}
