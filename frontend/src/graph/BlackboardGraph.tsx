import {
  Background,
  Controls,
  MiniMap,
  ReactFlow,
  reconnectEdge,
  useEdgesState,
  useNodesState,
  type Connection,
  type Edge,
  type Node,
  type ReactFlowInstance,
} from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import { useEffect, useMemo, useState } from 'react'

import type {
  BlackboardEdge,
  BlackboardNode as BlackboardNodeModel,
  GraphTool,
  LayoutSnapshot,
  WorkspaceMode,
} from '../types'
import { BlackboardNode, type BlackboardFlowNode } from './BlackboardNode'
import { IntentEdge, type IntentFlowEdge } from './IntentEdge'

export interface ReconnectCommand {
  edgeId: string
  source: string
  target: string
}

export type BlackboardFlowInstance = ReactFlowInstance<BlackboardFlowNode, Edge>

export function shouldFitAfterNodeChange(previousCount: number, nextCount: number) {
  return previousCount > 0 && nextCount > previousCount
}

export function advanceClickConnection(
  tool: GraphTool,
  source: string | null,
  clickedNodeId: string,
): {
  source: string | null
  command: { source: string; target: string } | null
} {
  if (tool !== 'connect') return { source: null, command: null }
  if (!source || source === clickedNodeId) {
    return { source: clickedNodeId, command: null }
  }
  return {
    source: null,
    command: { source, target: clickedNodeId },
  }
}

export function buildReconnectCommand(
  mode: WorkspaceMode,
  edgeId: string,
  source: string,
  target: string,
): ReconnectCommand | null {
  return mode === 'edit' ? { edgeId, source, target } : null
}

export function buildFlowEdges(edges: BlackboardEdge[]): Edge[] {
  return edges.map(edge => ({
    id: edge.id,
    type: edge.type === 'intent' ? 'intent' : 'default',
    source: edge.from_node,
    target: edge.to_node,
    label: edge.type === 'intent' ? undefined : edge.type,
    data: { edge },
    reconnectable: edge.type === 'intent',
  }))
}

function autoPosition(node: BlackboardNodeModel, index: number) {
  const column = Math.max(0, node.generation)
  const row = index % 5
  return { x: 80 + column * 260, y: 60 + row * 150 }
}

export function buildFlowNodes(
  nodes: BlackboardNodeModel[],
  layout: LayoutSnapshot,
  highestGeneration: number,
  connectingNodeId: string | null = null,
): BlackboardFlowNode[] {
  return nodes.filter(node => node.state !== 'archived').map((node, index) => ({
    id: node.id,
    type: 'blackboard',
    position: layout.nodes[node.id] ?? autoPosition(node, index),
    data: {
      node,
      isLatest: node.generation === highestGeneration,
      isConnecting: node.id === connectingNodeId,
    },
  }))
}

const nodeTypes = { blackboard: BlackboardNode }
const edgeTypes = { intent: IntentEdge }

export function BlackboardGraph({
  nodes: sourceNodes,
  edges: sourceEdges,
  layout,
  highestGeneration,
  mode,
  connectingNodeId,
  onSelect,
  onLayoutChange,
  onReconnectIntent,
  onConnectNodes,
  onInstance,
}: {
  nodes: BlackboardNodeModel[]
  edges: BlackboardEdge[]
  layout: LayoutSnapshot
  highestGeneration: number
  mode: WorkspaceMode
  connectingNodeId?: string | null
  onSelect: (selection: { kind: 'node'; value: BlackboardNodeModel } | { kind: 'edge'; value: BlackboardEdge } | null) => void
  onLayoutChange: (nodeId: string, position: { x: number; y: number }) => void
  onReconnectIntent: (command: ReconnectCommand) => void
  onConnectNodes: (source: string, target: string) => void
  onInstance?: (instance: BlackboardFlowInstance) => void
}) {
  const initialNodes = useMemo(
    () => buildFlowNodes(sourceNodes, layout, highestGeneration, connectingNodeId),
    [sourceNodes, layout, highestGeneration, connectingNodeId],
  )
  const initialEdges = useMemo(() => buildFlowEdges(sourceEdges), [sourceEdges])
  const [nodes, setNodes, onNodesChange] = useNodesState(initialNodes)
  const [edges, setEdges, onEdgesChange] = useEdgesState(initialEdges)

  useEffect(() => setNodes(initialNodes), [initialNodes, setNodes])
  useEffect(() => setEdges(initialEdges), [initialEdges, setEdges])

  function handleReconnect(oldEdge: Edge, connection: Connection) {
    if (!connection.source || !connection.target) return
    const command = buildReconnectCommand(mode, oldEdge.id, connection.source, connection.target)
    if (!command) return
    setEdges(current => reconnectEdge(oldEdge, connection, current))
    onReconnectIntent(command)
  }

  return (
    <div className="graph-canvas">
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        edgeTypes={edgeTypes}
        onNodesChange={onNodesChange}
        onEdgesChange={onEdgesChange}
        onNodeClick={(_, node) => onSelect({ kind: 'node', value: node.data.node as BlackboardNodeModel })}
        onEdgeClick={(_, edge) => onSelect({ kind: 'edge', value: edge.data?.edge as BlackboardEdge })}
        onPaneClick={() => onSelect(null)}
        onNodeDragStop={(_, node) => onLayoutChange(node.id, node.position)}
        onReconnect={handleReconnect}
        onConnect={connection => connection.source && connection.target && onConnectNodes(connection.source, connection.target)}
        nodesConnectable={mode === 'edit'}
        edgesReconnectable={mode === 'edit'}
        onInit={onInstance}
        fitView
        minZoom={0.15}
        maxZoom={2}
      >
        <Background gap={20} size={1} color="#c8ceca" />
        <Controls showInteractive={false} />
        <MiniMap pannable zoomable nodeStrokeWidth={3} />
      </ReactFlow>
    </div>
  )
}
