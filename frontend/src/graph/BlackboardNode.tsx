import { Handle, Position, type Node, type NodeProps } from '@xyflow/react'

import type { BlackboardNode as BlackboardNodeModel } from '../types'

export interface BlackboardNodeData extends Record<string, unknown> {
  node: BlackboardNodeModel
  isLatest: boolean
}

export type BlackboardFlowNode = Node<BlackboardNodeData, 'blackboard'>

const typeLabel = { goal: 'GOAL', origin: 'ORIGIN', fact: 'FACT', hint: 'HINT' }

export function BlackboardNode({ data, selected }: NodeProps<BlackboardFlowNode>) {
  const node = data.node
  return (
    <article className={`blackboard-node blackboard-node--${node.type} ${data.isLatest ? 'is-latest' : ''} ${selected ? 'is-selected' : ''}`}>
      <Handle type="target" position={Position.Left} />
      <div className="blackboard-node__meta">G{node.generation} · {typeLabel[node.type]}</div>
      <div className="blackboard-node__content">{node.content}</div>
      <div className="blackboard-node__sources">{node.sources.join(' · ')}</div>
      <Handle type="source" position={Position.Right} />
    </article>
  )
}
