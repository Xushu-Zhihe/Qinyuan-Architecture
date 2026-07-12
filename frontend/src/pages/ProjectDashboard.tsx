import { Plus, Search } from 'lucide-react'
import { useMemo, useState } from 'react'

import { ProjectCard } from '../components/ProjectCard'
import type { ProjectSummary } from '../types'

type Filter = 'all' | 'running' | 'manual' | 'achieved'

export function ProjectDashboard({
  projects,
  onOpen,
  onCreate,
}: {
  projects: ProjectSummary[]
  onOpen: (id: string) => void
  onCreate: () => void
}) {
  const [filter, setFilter] = useState<Filter>('all')
  const [query, setQuery] = useState('')
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
          <ProjectCard key={project.id} project={project} onOpen={onOpen} />
        ))}
        {visible.length === 0 && <p className="empty-state">没有符合条件的项目。</p>}
      </section>
    </main>
  )
}
