import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { api } from '../api/client'
import type {
  AutomationManifestDocument,
  AutomationRunSnapshot,
} from '../types'
import { AutomationPage } from './AutomationPage'

vi.mock('../api/client', () => ({
  api: {
    getCurrentAutomationRun: vi.fn(),
    parseAutomationManifest: vi.fn(),
    updateAutomationManifest: vi.fn(),
    validateAutomationManifest: vi.fn(),
    startAutomation: vi.fn(),
    stopAutomation: vi.fn(),
    sendAutomationInstruction: vi.fn(),
  },
}))

const manifest: AutomationManifestDocument = {
  manifest_id: 'manifest-1',
  manifest_hash: 'hash-1',
  manifest: { manifest_id: 'manifest-1' },
  json: '{"manifest_id":"manifest-1"}',
  markdown: '# Manifest',
}

const running: AutomationRunSnapshot = {
  run_id: 'run-1',
  manifest_id: 'manifest-1',
  manifest_hash: 'hash-1',
  status: 'running',
  processed_tasks: 0,
  max_tasks: 10,
  current_question: null,
  workgroup_project_id: null,
  workgroup_result: null,
  stop_after_current: false,
  error: '',
  started_at: '2026-08-21T00:00:00Z',
  events: [],
}

describe('AutomationPage', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(api.getCurrentAutomationRun).mockRejectedValue(
      new Error('no automation run exists'),
    )
    vi.mocked(api.parseAutomationManifest).mockResolvedValue(manifest)
    vi.mocked(api.validateAutomationManifest).mockResolvedValue({
      valid: true,
      manifest_id: 'manifest-1',
      errors: [],
    })
    vi.mocked(api.startAutomation).mockResolvedValue(running)
    vi.mocked(api.sendAutomationInstruction).mockResolvedValue({
      ...running,
      events: [
        {
          event_id: 'event-1',
          event_type: 'message',
          command_id: null,
          operation_id: null,
          status: 'planned',
          detail: { original_instruction: '下一题不做了' },
          created_at: '2026-08-21T00:00:01Z',
        },
      ],
    })
  })

  it('keeps parser and dispatcher LLM settings independent', async () => {
    const user = userEvent.setup()
    render(<AutomationPage onBack={() => {}} onOpenProject={() => {}} />)

    await user.type(
      screen.getByLabelText('比赛手册 / Skills'),
      'GET https://platform.example/question',
    )
    await user.type(screen.getAllByLabelText('API Key')[0], 'parser-key')
    await user.click(screen.getAllByRole('button', { name: '一键增加超时' })[0])
    await user.click(screen.getByRole('button', { name: '解析并生成 Manifest' }))

    await waitFor(() => expect(api.parseAutomationManifest).toHaveBeenCalled())
    expect(api.parseAutomationManifest).toHaveBeenCalledWith(
      expect.objectContaining({
        parser_llm: expect.objectContaining({
          api_key: 'parser-key',
          timeout_seconds: 600,
          max_transport_retries: 2,
        }),
      }),
    )
    expect(await screen.findByText(/ID：manifest-1/)).toBeInTheDocument()

    await user.type(screen.getAllByLabelText('API Key')[1], 'dispatcher-key')
    await user.type(screen.getByLabelText('平台 Token'), 'platform-token')
    await user.click(screen.getAllByRole('button', { name: '一键增加超时' })[1])
    expect(screen.getByRole('button', { name: '启动调度' })).toBeDisabled()
    await user.click(screen.getByRole('button', { name: '校验执行条件' }))
    await waitFor(() => expect(api.validateAutomationManifest).toHaveBeenCalledWith('manifest-1'))
    await user.click(screen.getByRole('button', { name: '启动调度' }))

    await waitFor(() => expect(api.startAutomation).toHaveBeenCalled())
    expect(api.startAutomation).toHaveBeenCalledWith(
      expect.objectContaining({
        manifest_id: 'manifest-1',
        dispatcher_llm: expect.objectContaining({
          api_key: 'dispatcher-key',
          timeout_seconds: 600,
          max_transport_retries: 2,
        }),
        platform_token: 'platform-token',
      }),
    )
  })

  it('sends a human instruction and renders its explicit event', async () => {
    vi.mocked(api.getCurrentAutomationRun).mockResolvedValue(running)
    const user = userEvent.setup()
    render(<AutomationPage onBack={() => {}} onOpenProject={() => {}} />)

    await screen.findByText('running')
    await user.type(screen.getByLabelText('告诉调度器下一步'), '下一题不做了')
    await user.click(screen.getByRole('button', { name: '发送显式指令' }))

    await waitFor(() =>
      expect(api.sendAutomationInstruction).toHaveBeenCalledWith(
        'run-1',
        '下一题不做了',
      ),
    )
    expect(await screen.findByText(/下一题不做了/)).toBeInTheDocument()
  })
})
