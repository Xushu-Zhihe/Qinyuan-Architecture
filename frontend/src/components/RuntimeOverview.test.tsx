import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import type { RuntimeSnapshot } from '../types'
import { RuntimeOverview } from './RuntimeOverview'

const runtime: RuntimeSnapshot = {
  limits: { max_global_running: 8, max_project_running: 2, max_global_planning: 3, max_project_planning: 1, aging_seconds: 30 },
  coordinator: {
    running: { global: 1, planning: 0, execution: 1, by_project: { 'project-1': 1 }, by_provider: { codex: 1 } },
    pending: { total: 1, planning: 1, execution: 0, items: [{ project_id: 'project-1', provider: 'claude', kind: 'planning', priority: 5, queue_seconds: 2, wait_reasons: ['project_limit'] }] },
  },
  projects: [{
    id: 'project-1', name: 'Runtime project', mode: 'run', status: 'running', challenge_path: null,
    providers: ['codex'], agent_limit: 1, created_at: 'now', updated_at: 'now', last_opened_at: 'now',
    active_task_count: 1, recovery: { planning_attempts: 0, executions: 0, total: 0, latest_at: null },
    event_metrics: { subscribers: 1, queue_depth: 0, queue_capacity: 500, published_events: 3, dropped_events: 0, backfilled_events: 1, log_events_last_minute: 2 },
    provider_health: [{ provider: 'codex', status: 'healthy', consecutive_failures: 0, last_error: null, checked_at: 'now', cooldown_until: null, updated_at: 'now', cli_version: 'codex 1' }],
  }],
  active_tasks: [{ kind: 'execution', id: 'execution-1', project_id: 'project-1', project_name: 'Runtime project', provider: 'codex', status: 'running', queued_at: 'now', started_at: 'now', intent_id: 'intent-1' }],
}

describe('RuntimeOverview', () => {
  it('shows configured limits, queue reasons and provider health', () => {
    render(<RuntimeOverview runtime={runtime} />)

    expect(screen.getByText('/ 8', { exact: false })).toBeInTheDocument()
    expect(screen.getByText('project_limit')).toBeInTheDocument()
    expect(screen.getByText('codex · 健康')).toBeInTheDocument()
  })

  it('shows only the ten most recent projects in provider health', () => {
    const projects = Array.from({ length: 12 }, (_, index) => ({
      ...runtime.projects[0],
      id: `project-${index + 1}`,
      name: `Runtime project ${index + 1}`,
    }))

    render(<RuntimeOverview runtime={{ ...runtime, projects }} />)

    expect(screen.getByText('Runtime project 1')).toBeInTheDocument()
    expect(screen.getByText('Runtime project 10')).toBeInTheDocument()
    expect(screen.queryByText('Runtime project 11')).not.toBeInTheDocument()
    expect(screen.queryByText('Runtime project 12')).not.toBeInTheDocument()
  })
})
