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
}

export type IntentFlowEdge = Edge<IntentEdgeData, 'intent'>

export function IntentEdge(props: EdgeProps<IntentFlowEdge>) {
  const [path, labelX, labelY] = getBezierPath(props)
  const status = props.data?.edge.status ?? 'planned'
  return (
    <>
      <BaseEdge id={props.id} path={path} className={`intent-edge intent-edge--${status}`} markerEnd={props.markerEnd} />
      <EdgeLabelRenderer>
        <div
          className={`intent-label intent-label--${status}`}
          style={{ transform: `translate(-50%, -50%) translate(${labelX}px, ${labelY}px)` }}
        >
          {props.data?.edge.content ?? 'Intent'}
        </div>
      </EdgeLabelRenderer>
    </>
  )
}
