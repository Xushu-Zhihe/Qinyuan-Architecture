import { Focus, GitPullRequestArrow, MousePointer2, Plus } from 'lucide-react'

import type { WorkspaceMode } from '../types'

export function GraphToolbar({
  mode,
  onAddFact,
  onConnect,
  onFit,
}: {
  mode: WorkspaceMode
  onAddFact: () => void
  onConnect: () => void
  onFit: () => void
}) {
  const locked = mode === 'browse'
  return (
    <nav className="graph-toolbar" aria-label="图工具">
      <button className="graph-tool is-active" title="选择" aria-label="选择"><MousePointer2 size={18} /></button>
      <button className="graph-tool" title="添加 Fact" aria-label="添加 Fact" disabled={locked} onClick={onAddFact}><Plus size={18} /></button>
      <button className="graph-tool" title="连接节点" aria-label="连接节点" disabled={locked} onClick={onConnect}><GitPullRequestArrow size={18} /></button>
      <button className="graph-tool" title="适应画布" aria-label="适应画布" onClick={onFit}><Focus size={18} /></button>
    </nav>
  )
}
