import { Archive, CircleDot, GitBranch, History } from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'

import type {
  BlackboardEdge,
  BlackboardNode,
  IntentExecution,
  ProjectSnapshot,
} from '../types'

export type InspectorSelection =
  | { kind: 'node'; value: BlackboardNode }
  | { kind: 'edge'; value: BlackboardEdge }
  | null

interface InspectorPanelProps {
  selection: InspectorSelection
  snapshot: ProjectSnapshot
  onSelectExecution: (execution: IntentExecution | null) => void
  onArchiveNode?: (nodeId: string) => void
}

function providerLabel(provider: string) {
  return provider.charAt(0).toUpperCase() + provider.slice(1)
}

function nodeLabel(snapshot: ProjectSnapshot, nodeId: string) {
  const node = snapshot.nodes.find(item => item.id === nodeId)
  return node ? `${node.type.toUpperCase()} · ${node.content}` : nodeId
}

export function InspectorPanel({
  selection,
  snapshot,
  onSelectExecution,
  onArchiveNode,
}: InspectorPanelProps) {
  const selectedNode = selection?.kind === 'node'
    ? snapshot.nodes.find(node => node.id === selection.value.id) ?? selection.value
    : null
  const selectedEdge = selection?.kind === 'edge'
    ? snapshot.edges.find(edge => edge.id === selection.value.id) ?? selection.value
    : selectedNode?.produced_by
      ? snapshot.edges.find(edge => edge.id === selectedNode.produced_by?.intent_id) ?? null
      : null
  const executions = useMemo(() => selectedEdge?.executions ?? [], [selectedEdge])
  const preferredExecutionId = selectedNode?.produced_by?.execution_id
    ?? executions.find(execution => execution.status === 'running')?.id
    ?? executions[0]?.id
    ?? null
  const [activeExecutionId, setActiveExecutionId] = useState<string | null>(preferredExecutionId)

  useEffect(() => {
    setActiveExecutionId(preferredExecutionId)
    onSelectExecution(
      executions.find(execution => execution.id === preferredExecutionId) ?? null,
    )
  }, [executions, onSelectExecution, preferredExecutionId])

  function selectExecution(execution: IntentExecution) {
    setActiveExecutionId(execution.id)
    onSelectExecution(execution)
  }

  if (!selection) {
    return <aside className="inspector inspector--empty" aria-label="详情"><span>未选择</span></aside>
  }

  const relations = selectedNode
    ? snapshot.edges.filter(edge => edge.type !== 'intent' && (
      edge.from_node === selectedNode.id || edge.to_node === selectedNode.id
    ))
    : []
  const canArchive = selectedNode
    && selectedNode.sources.includes('human')
    && (selectedNode.type === 'fact' || selectedNode.type === 'hint')
    && selectedNode.state !== 'archived'

  return (
    <aside className="inspector" aria-label="详情">
      {selectedNode && (
        <>
          <div className="inspector-heading">
            <div>
              <span className="inspector-kicker">{selectedNode.type}</span>
              <h2>{selectedNode.id}</h2>
            </div>
            {canArchive && onArchiveNode && (
              <button
                className="icon-button"
                aria-label="归档节点"
                title="归档节点"
                onClick={() => onArchiveNode(selectedNode.id)}
              >
                <Archive size={17} />
              </button>
            )}
          </div>
          <p className="inspector-content">{selectedNode.content}</p>
          <dl className="inspector-facts">
            <div><dt>Generation</dt><dd>G{selectedNode.generation}</dd></div>
            <div><dt>状态</dt><dd>{selectedNode.state}</dd></div>
            <div><dt>来源</dt><dd>{selectedNode.sources.join(' · ') || 'system'}</dd></div>
          </dl>
          {relations.length > 0 && (
            <section className="inspector-section">
              <h3><GitBranch size={14} /> 关系</h3>
              <ul className="relation-list">
                {relations.map(edge => (
                  <li key={edge.id}>
                    <span>{edge.type}</span>
                    <small>{edge.from_node === selectedNode.id ? edge.to_node : edge.from_node}</small>
                  </li>
                ))}
              </ul>
            </section>
          )}
        </>
      )}

      {selectedEdge && (
        <section className="inspector-section intent-detail">
          <div className="inspector-heading">
            <div>
              <span className="inspector-kicker">{selectedEdge.status ?? selectedEdge.type}</span>
              <h2>Intent {selectedEdge.id}</h2>
            </div>
            <CircleDot size={18} />
          </div>
          <p className="inspector-content">{selectedEdge.content}</p>
          <dl className="inspector-facts">
            <div><dt>起点</dt><dd title={selectedEdge.from_node}>{nodeLabel(snapshot, selectedEdge.from_node)}</dd></div>
            <div><dt>目标</dt><dd title={selectedEdge.to_node}>{nodeLabel(snapshot, selectedEdge.to_node)}</dd></div>
            <div><dt>来源</dt><dd>{selectedEdge.sources.join(' · ') || 'system'}</dd></div>
          </dl>
          {executions.length > 0 && (
            <section className="execution-section">
              <h3><History size={14} /> Executions</h3>
              <div className="execution-tabs" role="tablist" aria-label="Agent executions">
                {executions.map(execution => (
                  <button
                    key={execution.id}
                    role="tab"
                    aria-selected={execution.id === activeExecutionId}
                    className={execution.id === activeExecutionId ? 'execution-tab is-active' : 'execution-tab'}
                    onClick={() => selectExecution(execution)}
                  >
                    {providerLabel(execution.provider)}
                    <span className={`execution-dot execution-dot--${execution.status}`} />
                  </button>
                ))}
              </div>
              {executions.find(execution => execution.id === activeExecutionId) && (
                <dl className="execution-summary">
                  <div><dt>执行</dt><dd>{activeExecutionId}</dd></div>
                  <div><dt>状态</dt><dd>{executions.find(execution => execution.id === activeExecutionId)?.status}</dd></div>
                </dl>
              )}
            </section>
          )}
        </section>
      )}
    </aside>
  )
}
