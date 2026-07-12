import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import { NewProjectDialog } from './NewProjectDialog'

async function fillRequiredFields() {
  await userEvent.type(screen.getByLabelText('项目名称'), 'Manual Board')
  await userEvent.type(screen.getByLabelText('Goal'), 'Recover the flag')
  await userEvent.type(screen.getByLabelText('Origin'), 'Challenge statement')
}

describe('NewProjectDialog', () => {
  it('creates a manual project without providers', async () => {
    const onSubmit = vi.fn()
    render(
      <NewProjectDialog
        open
        providers={['codex', 'claude']}
        onSubmit={onSubmit}
        onClose={() => {}}
      />,
    )
    await fillRequiredFields()
    await userEvent.click(screen.getByLabelText('仅黑板'))
    await userEvent.click(screen.getByRole('button', { name: '创建项目' }))

    expect(onSubmit).toHaveBeenCalledWith(
      expect.objectContaining({ mode: 'manual', providers: [] }),
    )
  })

  it('submits selected providers in run mode', async () => {
    const onSubmit = vi.fn()
    render(
      <NewProjectDialog
        open
        providers={['codex', 'claude']}
        onSubmit={onSubmit}
        onClose={() => {}}
      />,
    )
    await fillRequiredFields()
    await userEvent.click(screen.getByLabelText('立即启动 Agent'))
    await userEvent.click(screen.getByLabelText('codex'))
    await userEvent.click(screen.getByRole('button', { name: '创建并启动' }))

    expect(onSubmit).toHaveBeenCalledWith(
      expect.objectContaining({ mode: 'run', providers: ['codex'] }),
    )
  })

  it('keeps selected providers for a manual project that can start later', async () => {
    const onSubmit = vi.fn()
    render(
      <NewProjectDialog
        open
        providers={['codex', 'claude']}
        onSubmit={onSubmit}
        onClose={() => {}}
      />,
    )
    await fillRequiredFields()
    await userEvent.click(screen.getByLabelText('codex'))
    await userEvent.click(screen.getByRole('button', { name: '创建项目' }))

    expect(onSubmit).toHaveBeenCalledWith(
      expect.objectContaining({ mode: 'manual', providers: ['codex'] }),
    )
  })
})
