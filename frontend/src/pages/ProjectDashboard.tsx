import { Plus, Search, Trash2, X } from 'lucide-react'
import { useMemo, useState } from 'react'

import { ProjectCard } from '../components/ProjectCard'
import { RuntimeOverview } from '../components/RuntimeOverview'
import type { ProjectSummary, RuntimeSnapshot } from '../types'

type Filter = 'all' | 'running' | 'manual' | 'achieved'

export function ProjectDashboard({
  projects,
  onOpen,
  onCreate,
  onDelete,
  runtime,
}: {
  projects: ProjectSummary[]
  onOpen: (id: string) => void
  onCreate: () => void
  onDelete: (projectId: string) => Promise<void>
  runtime: RuntimeSnapshot | null
}) {
  const [filter, setFilter] = useState<Filter>('all')
  const [query, setQuery] = useState('')
  const [pendingDelete, setPendingDelete] = useState<ProjectSummary | null>(null)
  const [deleting, setDeleting] = useState(false)
  const [deleteError, setDeleteError] = useState<string | null>(null)
  const visible = useMemo(() => {
    const normalized = query.trim().toLocaleLowerCase()
    return projects.filter(project => {
      const matchesFilter =
        filter === 'all' ||
        (filter === 'running' && project.status === 'running') ||
        (filter === 'manual' && project.mode === 'manual') ||
        (filter === 'achieved' && project.status === 'achieved')
      const matchesQuery = !normalized || project.name.toLocaleLowerCase().includes(normalized)
      return matchesFilter && matchesQuery
    })
  }, [filter, projects, query])

  async function confirmDelete() {
    if (!pendingDelete || deleting) return
    try {
      setDeleting(true)
      setDeleteError(null)
      await onDelete(pendingDelete.id)
      setPendingDelete(null)
    } catch (caught) {
      setDeleteError(caught instanceof Error ? caught.message : String(caught))
    } finally {
      setDeleting(false)
    }
  }

  return (
    <main className="dashboard-shell">
      <header className="dashboard-header">
        <div>
          <div className="brand">BLACKBOARD</div>
          <p>{projects.filter(project => project.status === 'running').length} 个项目运行中</p>
        </div>
        <button className="primary-button" onClick={onCreate}>
          <Plus size={18} /> 新建项目
        </button>
      </header>
      <RuntimeOverview runtime={runtime} />
      <section className="dashboard-controls" aria-label="项目筛选">
        <div className="filter-tabs">
          {([
            ['all', '全部'],
            ['running', '运行中'],
            ['manual', '仅黑板'],
            ['achieved', '已完成'],
          ] as const).map(([value, label]) => (
            <button
              key={value}
              className={filter === value ? 'filter-tab is-active' : 'filter-tab'}
              onClick={() => setFilter(value)}
            >
              {label}
            </button>
          ))}
        </div>
        <label className="search-field">
          <Search size={17} />
          <span className="sr-only">搜索项目</span>
          <input
            value={query}
            onChange={event => setQuery(event.target.value)}
            placeholder="搜索项目"
          />
        </label>
      </section>
      <section className="project-grid" aria-label="项目列表">
        {visible.map(project => (
          <ProjectCard
            key={project.id}
            project={project}
            onOpen={onOpen}
            onDelete={selected => { setDeleteError(null); setPendingDelete(selected) }}
          />
        ))}
        {visible.length === 0 && <p className="empty-state">没有符合条件的项目。</p>}
      </section>
      {pendingDelete && (
        <div className="dialog-backdrop" role="presentation">
          <section className="project-dialog delete-dialog" role="dialog" aria-modal="true" aria-labelledby="delete-project-title">
            <header className="dialog-header">
              <div>
                <h2 id="delete-project-title">删除项目</h2>
                <p>{pendingDelete.name} · {statusLabel[pendingDelete.status] ?? pendingDelete.status}</p>
              </div>
              <button className="icon-button" aria-label="关闭" disabled={deleting} onClick={() => setPendingDelete(null)}><X size={20} /></button>
            </header>
            <div className="delete-dialog__body">
              <Trash2 size={20} />
              <p>项目会先停止所有 Agent，再移动到数据目录的 <code>.trash</code>。Challenge 与外部 workspace 不会被移动。</p>
              {pendingDelete.status === 'running' && <p className="delete-dialog__warning">该项目正在运行，删除前会等待 Agent 停止并完成清理。</p>}
              {deleteError && <p className="form-error" role="alert">{deleteError}</p>}
            </div>
            <footer className="dialog-actions">
              <button className="secondary-button" disabled={deleting} onClick={() => setPendingDelete(null)}>取消</button>
              <button className="danger-button" disabled={deleting} onClick={() => void confirmDelete()}>{deleting ? '正在删除...' : '移入回收目录'}</button>
            </footer>
          </section>
        </div>
      )}
    </main>
  )
}

const statusLabel: Record<string, string> = {
  running: '运行中',
  idle: '仅黑板',
  degraded: '有失败',
  achieved: '已完成',
  stopped: '已停止',
}
