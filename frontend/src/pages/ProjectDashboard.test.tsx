import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import type { ProjectSummary } from '../types'
import { ProjectDashboard } from './ProjectDashboard'

const projects: ProjectSummary[] = [
  {
    id: 'project-web',
    name: 'Web Challenge 17',
    mode: 'run',
    status: 'running',
    challenge_path: null,
    providers: ['codex'],
    agent_limit: 1,
    created_at: '2026-07-12',
    updated_at: '2026-07-12',
    last_opened_at: '2026-07-12',
    fact_count: 18,
    intent_count: 7,
    highest_generation: 4,
  },
  {
    id: 'project-manual',
    name: 'Manual Board',
    mode: 'manual',
    status: 'idle',
    challenge_path: null,
    providers: [],
    agent_limit: 1,
    created_at: '2026-07-12',
    updated_at: '2026-07-12',
    last_opened_at: '2026-07-12',
  },
  {
    id: 'project-starting',
    name: 'Starting Agent',
    mode: 'run',
    status: 'idle',
    challenge_path: null,
    providers: ['qinyuan-agent'],
    agent_limit: 1,
    created_at: '2026-07-12',
    updated_at: '2026-07-12',
    last_opened_at: '2026-07-12',
  },
]

describe('ProjectDashboard', () => {
  it('does not label a run-mode project waiting for health as manual', () => {
    render(
      <ProjectDashboard
        projects={[projects[2]]}
        runtime={null}
        onOpen={() => {}}
        onCreate={() => {}}
        onDelete={async () => {}}
        onOpenAutomation={() => {}}
      />,
    )

    expect(screen.getByText('启动中')).toBeInTheDocument()
  })

  it('filters running projects and opens the selected project', async () => {
    const onOpen = vi.fn()
    render(
      <ProjectDashboard
        projects={projects}
        runtime={null}
        onOpen={onOpen}
        onCreate={() => {}}
        onDelete={async () => {}}
        onOpenAutomation={() => {}}
      />,
    )

    await userEvent.click(screen.getByRole('button', { name: '运行中' }))
    expect(screen.queryByText('Manual Board')).not.toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Web Challenge 17，运行中' }))
    expect(onOpen).toHaveBeenCalledWith('project-web')
  })

  it('confirms project deletion without opening the project', async () => {
    const onOpen = vi.fn()
    const onDelete = vi.fn().mockResolvedValue(undefined)
    render(
      <ProjectDashboard
        projects={projects}
        runtime={null}
        onOpen={onOpen}
        onCreate={() => {}}
        onDelete={onDelete}
        onOpenAutomation={() => {}}
      />,
    )

    await userEvent.click(screen.getByRole('button', { name: '删除项目 Manual Board' }))
    expect(screen.getByRole('dialog', { name: '删除项目' })).toHaveTextContent('Manual Board')
    await userEvent.click(screen.getByRole('button', { name: '移入回收目录' }))

    expect(onDelete).toHaveBeenCalledWith('project-manual')
    expect(onOpen).not.toHaveBeenCalled()
  })

  it('keeps the delete dialog open when the server rejects deletion', async () => {
    render(
      <ProjectDashboard
        projects={projects}
        runtime={null}
        onOpen={() => {}}
        onCreate={() => {}}
        onDelete={async () => { throw new Error('project still has active work') }}
        onOpenAutomation={() => {}}
      />,
    )

    await userEvent.click(screen.getByRole('button', { name: '删除项目 Web Challenge 17' }))
    await userEvent.click(screen.getByRole('button', { name: '移入回收目录' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('project still has active work')
    expect(screen.getByRole('dialog')).toBeInTheDocument()
  })
})
