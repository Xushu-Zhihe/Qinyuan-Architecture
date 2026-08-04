import { Activity, ArrowUpRight, Bot, GitBranch, Trash2 } from 'lucide-react'

import type { ProjectSummary } from '../types'

const statusLabel: Record<string, string> = {
  running: '运行中',
  idle: '仅黑板',
  degraded: '有失败',
  achieved: '已完成',
  stopped: '已停止',
}

export function ProjectCard({
  project,
  onOpen,
  onDelete,
}: {
  project: ProjectSummary
  onOpen: (id: string) => void
  onDelete: (project: ProjectSummary) => void
}) {
  return (
    <article className="project-card">
      <button
        className="project-card__open"
        onClick={() => onOpen(project.id)}
        aria-label={`${project.name}，${statusLabel[project.status] ?? project.status}`}
      >
        <span className="project-card__topline">
          <span className="project-card__name">{project.name}</span>
          <span className={`status status--${project.status}`}>
            {statusLabel[project.status] ?? project.status}
          </span>
        </span>
        <span className="project-card__summary">
          {project.mode === 'manual' ? '人工黑板，可随时启动 Agent' : '多 Agent 异步探索任务'}
        </span>
        <span className="project-metrics">
          <span><Activity size={15} /> <strong>{project.fact_count ?? 0}</strong> Fact</span>
          <span><GitBranch size={15} /> <strong>{project.intent_count ?? 0}</strong> Intent</span>
          <span><Bot size={15} /> G{project.highest_generation ?? 0}</span>
        </span>
        <span className="project-card__footer">
          <span>{project.providers.length ? project.providers.join(' · ') : '未配置 Agent'}</span>
          <ArrowUpRight size={17} aria-hidden="true" />
        </span>
      </button>
      <button
        className="project-card__delete icon-button"
        aria-label={`删除项目 ${project.name}`}
        title="删除项目"
        onClick={() => onDelete(project)}
      >
        <Trash2 size={16} />
      </button>
    </article>
  )
}
