import { ChevronDown, ChevronUp } from 'lucide-react'
import { useState, type MouseEvent } from 'react'
import { Handle, Position, type Node, type NodeProps } from '@xyflow/react'

import type { BlackboardNode as BlackboardNodeModel } from '../types'
import { estimateTokenCount } from '../utils/tokenEstimate'

export interface BlackboardNodeData extends Record<string, unknown> {
  node: BlackboardNodeModel
  isLatest: boolean
  isConnecting: boolean
}

export type BlackboardFlowNode = Node<BlackboardNodeData, 'blackboard'>

const typeLabel = { goal: 'GOAL', origin: 'ORIGIN', fact: 'FACT', hint: 'HINT' }

export function BlackboardNode({ data, selected }: NodeProps<BlackboardFlowNode>) {
  const node = data.node
  const isFact = node.type === 'fact'
  const [expanded, setExpanded] = useState(false)
  const tokenCount = isFact ? estimateTokenCount(node.content) : null

  function toggleExpanded(event: MouseEvent<HTMLButtonElement>) {
    event.stopPropagation()
    setExpanded(value => !value)
  }

  return (
    <article className={`blackboard-node blackboard-node--${node.type} ${node.state === 'archived' ? 'is-archived' : ''} ${data.isLatest ? 'is-latest' : ''} ${data.isConnecting ? 'is-connecting' : ''} ${selected ? 'is-selected' : ''}`}>
      <Handle type="target" position={Position.Left} />
      <div className="blackboard-node__meta">
        <span>G{node.generation} · {typeLabel[node.type]}</span>
        {isFact && <span className="blackboard-node__token">~{tokenCount} tokens</span>}
      </div>
      <div className={`blackboard-node__content ${isFact && !expanded ? 'is-collapsed' : ''}`}>{node.content}</div>
      {isFact && <button
        type="button"
        className="blackboard-node__toggle"
        aria-label={expanded ? '折叠 Fact 内容' : '展开 Fact 内容'}
        aria-expanded={expanded}
        title={expanded ? '折叠 Fact 内容' : '展开 Fact 内容'}
        onPointerDown={event => event.stopPropagation()}
        onClick={toggleExpanded}
      >
        {expanded ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
      </button>}
      <div className="blackboard-node__sources">{node.sources.join(' · ')}</div>
      <Handle type="source" position={Position.Right} />
    </article>
  )
}
