import { Archive, Focus, GitPullRequestArrow, MousePointer2, Plus, RotateCcw, Trash2 } from 'lucide-react'

import type { GraphTool, WorkspaceMode } from '../types'

export function GraphToolbar({
  mode,
  activeTool,
  onSelect,
  onAddFact,
  onConnect,
  removeKind,
  onRemove,
  onFit,
}: {
  mode: WorkspaceMode
  activeTool: GraphTool
  onSelect: () => void
  onAddFact: () => void
  onConnect: () => void
  removeKind: 'archive' | 'restore' | 'delete' | null
  onRemove: () => void
  onFit: () => void
}) {
  const locked = mode === 'browse'
  return (
    <nav className="graph-toolbar" aria-label="图工具">
      <button className={activeTool === 'select' ? 'graph-tool is-active' : 'graph-tool'} title="选择" aria-label="选择" onClick={onSelect}><MousePointer2 size={18} /></button>
      <button className="graph-tool" title="添加 Fact" aria-label="添加 Fact" disabled={locked} onClick={onAddFact}><Plus size={18} /></button>
      <button className={activeTool === 'connect' ? 'graph-tool is-active' : 'graph-tool'} title="连接节点" aria-label="连接节点" disabled={locked} onClick={onConnect}><GitPullRequestArrow size={18} /></button>
      <button
        className="graph-tool"
        title={removeKind === 'archive' ? '归档节点' : removeKind === 'restore' ? '恢复节点' : removeKind === 'delete' ? '删除边' : '选择可操作的节点或边'}
        aria-label={removeKind === 'archive' ? '归档节点' : removeKind === 'restore' ? '恢复节点' : removeKind === 'delete' ? '删除边' : '删除或归档'}
        disabled={locked || removeKind === null}
        onClick={onRemove}
      >
        {removeKind === 'archive' ? <Archive size={18} /> : removeKind === 'restore' ? <RotateCcw size={18} /> : <Trash2 size={18} />}
      </button>
      <button className="graph-tool" title="适应画布" aria-label="适应画布" onClick={onFit}><Focus size={18} /></button>
    </nav>
  )
}
