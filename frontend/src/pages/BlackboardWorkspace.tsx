import { ArrowLeft, Eye, Pencil, Play, Square } from 'lucide-react'
import { useEffect, useReducer, useRef, useState } from 'react'

import { api } from '../api/client'
import { connectProjectSocket } from '../api/projectSocket'
import { GraphToolbar } from '../components/GraphToolbar'
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
import type { GraphTool, IntentExecution, WorkspaceMode } from '../types'

export function BlackboardWorkspace({ projectId, onBack }: { projectId: string; onBack: () => void }) {
  const [state, dispatch] = useReducer((current: ReturnType<typeof createProjectState>, action: { type: 'snapshot'; value: any } | { type: 'event'; value: any }) => action.type === 'snapshot' ? replaceSnapshot(current, action.value) : reduceProjectEvent(current, action.value), createProjectState())
  const [mode, setMode] = useState<WorkspaceMode>('browse')
  const [activeTool, setActiveTool] = useState<GraphTool>('select')
  const [connectSource, setConnectSource] = useState<string | null>(null)
  const [selection, setSelection] = useState<InspectorSelection>(null)
  const [selectedExecution, setSelectedExecution] = useState<IntentExecution | null>(null)
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
    if (shouldFit) {
      requestAnimationFrame(() => { void flow.fitView({ padding: .18, duration: 180 }) })
    }
  }, [flow, snapshot?.nodes.length])

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

  return (
    <main className="workspace-shell">
      <header className="workspace-header">
        <button className="icon-button" aria-label="返回项目总览" onClick={onBack}><ArrowLeft size={20} /></button>
        <div className="workspace-title"><strong>{snapshot.project.name}</strong><span className={`status status--${snapshot.project.status}`}>{snapshot.project.status}</span></div>
        <div className="generation-chip">最新一轮 · G{snapshot.highest_generation}</div>
        <div className="workspace-actions">
          <button className={mode === 'browse' ? 'mode-button is-active' : 'mode-button'} onClick={() => changeMode('browse')}><Eye size={16} /> 浏览</button>
          <button className={mode === 'edit' ? 'mode-button is-active' : 'mode-button'} onClick={() => changeMode('edit')}><Pencil size={16} /> 编辑</button>
          {snapshot.project.status === 'running' ? <button className="secondary-button" onClick={() => void api.stopProject(projectId).then(refresh)}><Square size={15} /> 停止</button> : <button className="primary-button" onClick={() => void api.startProject(projectId).then(refresh)}><Play size={15} /> 启动 Agent</button>}
        </div>
      </header>
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
