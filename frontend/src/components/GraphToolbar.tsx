import { Focus, GitPullRequestArrow, MousePointer2, Plus } from 'lucide-react'

import type { GraphTool, WorkspaceMode } from '../types'

export function GraphToolbar({
  mode,
  activeTool,
  onSelect,
  onAddFact,
  onConnect,
  onFit,
}: {
  mode: WorkspaceMode
  activeTool: GraphTool
  onSelect: () => void
  onAddFact: () => void
  onConnect: () => void
  onFit: () => void
}) {
  const locked = mode === 'browse'
  return (
    <nav className="graph-toolbar" aria-label="图工具">
      <button className={activeTool === 'select' ? 'graph-tool is-active' : 'graph-tool'} title="选择" aria-label="选择" onClick={onSelect}><MousePointer2 size={18} /></button>
      <button className="graph-tool" title="添加 Fact" aria-label="添加 Fact" disabled={locked} onClick={onAddFact}><Plus size={18} /></button>
      <button className={activeTool === 'connect' ? 'graph-tool is-active' : 'graph-tool'} title="连接节点" aria-label="连接节点" disabled={locked} onClick={onConnect}><GitPullRequestArrow size={18} /></button>
      <button className="graph-tool" title="适应画布" aria-label="适应画布" onClick={onFit}><Focus size={18} /></button>
    </nav>
  )
}
