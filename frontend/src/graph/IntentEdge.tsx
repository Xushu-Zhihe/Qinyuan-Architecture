import {
  BaseEdge,
  EdgeLabelRenderer,
  getBezierPath,
  type Edge,
  type EdgeProps,
} from '@xyflow/react'

import type { BlackboardEdge } from '../types'

export interface IntentEdgeData extends Record<string, unknown> {
  edge: BlackboardEdge
  onSelect?: (edge: BlackboardEdge) => void
}

export type IntentFlowEdge = Edge<IntentEdgeData, 'intent'>

export function IntentEdge(props: EdgeProps<IntentFlowEdge>) {
  const [path, labelX, labelY] = getBezierPath(props)
  const status = props.data?.edge.status ?? 'planned'
  return (
    <>
      <BaseEdge id={props.id} path={path} className={`intent-edge intent-edge--${status}`} markerEnd={props.markerEnd} />
      <EdgeLabelRenderer>
        <button
          type="button"
          aria-label={`选择 Intent ${props.data?.edge.content ?? props.id}`}
          className={`intent-label intent-label--${status}`}
          style={{ transform: `translate(-50%, -50%) translate(${labelX}px, ${labelY}px)` }}
          onClick={event => {
            event.stopPropagation()
            if (props.data?.edge) props.data.onSelect?.(props.data.edge)
          }}
        >
          {props.data?.edge.content ?? 'Intent'}
        </button>
      </EdgeLabelRenderer>
    </>
  )
}
