import { Activity, Clock3, Gauge, Server } from 'lucide-react'

import type { RuntimeSnapshot } from '../types'

const statusLabel: Record<string, string> = {
  healthy: '健康',
  unhealthy: '不可用',
  auth_failed: '认证失败',
  missing: '命令缺失',
  cooldown: '冷却中',
  unknown: '未探测',
}

const kindLabel = { planning: '规划', execution: '执行' }

export function RuntimeOverview({ runtime }: { runtime: RuntimeSnapshot | null }) {
  if (!runtime) {
    return <section className="runtime-overview runtime-overview--loading" aria-label="全局运行状态">正在读取运行状态...</section>
  }

  const projectNames = new Map(runtime.projects.map(project => [project.id, project.name]))
  const active = runtime.active_tasks.slice(0, 4)
  const pending = runtime.coordinator.pending.items.slice(0, 4)

  return (
    <section className="runtime-overview" aria-label="全局运行状态">
      <header className="runtime-heading">
        <div>
          <span className="runtime-kicker"><Activity size={14} /> RUN TIME</span>
          <h2>运行总览</h2>
        </div>
        <span className="runtime-live"><span /> 自动刷新</span>
      </header>
      <dl className="runtime-metrics">
        <div><dt><Gauge size={15} /> 运行</dt><dd>{runtime.coordinator.running.global}<small> / {runtime.limits.max_global_running}</small></dd></div>
        <div><dt><Clock3 size={15} /> 排队</dt><dd>{runtime.coordinator.pending.total}</dd></div>
        <div><dt><Server size={15} /> 项目上限</dt><dd>{runtime.limits.max_project_running}</dd></div>
        <div><dt>规划</dt><dd>{runtime.coordinator.running.planning}<small> / {runtime.limits.max_global_planning}</small></dd></div>
      </dl>
      <div className="runtime-columns">
        <section>
          <h3>活动任务</h3>
          <ul className="runtime-task-list">
            {active.map(task => (
              <li key={`${task.kind}-${task.id}`}>
                <span className={`runtime-kind runtime-kind--${task.kind}`}>{kindLabel[task.kind]}</span>
                <strong title={task.project_name}>{task.project_name}</strong>
                <span>{task.provider}</span>
                <small>{task.status}</small>
              </li>
            ))}
            {active.length === 0 && <li className="runtime-empty">当前无活动任务</li>}
          </ul>
        </section>
        <section>
          <h3>等待队列</h3>
          <ul className="runtime-task-list">
            {pending.map((task, index) => (
              <li key={`${task.project_id}-${task.provider}-${task.kind}-${index}`}>
                <span className={`runtime-kind runtime-kind--${task.kind}`}>{kindLabel[task.kind]}</span>
                <strong title={projectNames.get(task.project_id)}>{projectNames.get(task.project_id) ?? task.project_id}</strong>
                <span>{task.provider}</span>
                <small>{task.wait_reasons.join(' · ')}</small>
              </li>
            ))}
            {pending.length === 0 && <li className="runtime-empty">当前无排队任务</li>}
          </ul>
        </section>
      </div>
      <div className="runtime-provider-grid" aria-label="Provider 健康状态">
        {runtime.projects.map(project => (
          <div className="runtime-provider-row" key={project.id}>
            <strong title={project.name}>{project.name}</strong>
            <span>{project.active_task_count} 活动</span>
            <div>
              {project.provider_health.map(health => (
                <span
                  key={health.provider}
                  className={`health-chip health-chip--${health.status}`}
                  title={health.last_error ?? health.cli_version}
                >
                  {health.provider} · {statusLabel[health.status] ?? health.status}
                </span>
              ))}
              {project.provider_health.length === 0 && <span className="health-chip">未配置 Provider</span>}
            </div>
          </div>
        ))}
      </div>
    </section>
  )
}
