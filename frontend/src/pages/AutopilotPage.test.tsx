import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { api } from '../api/client'
import type { AutopilotSnapshot, StructuredPlatformInput } from '../types'
import { AutopilotPage } from './AutopilotPage'

vi.mock('../api/client', () => ({
  api: {
    getAutopilot: vi.fn(),
    listAutopilotTemplates: vi.fn(),
    saveAutopilotTemplate: vi.fn(),
    deleteAutopilotTemplate: vi.fn(),
    startAutopilot: vi.fn(),
    stopAutopilot: vi.fn(),
    extendAutopilotTime: vi.fn(),
    skipAutopilotTask: vi.fn(),
    testAutopilotController: vi.fn(),
    validateAutopilotPlatform: vi.fn(),
    validateAutopilotPrompt: vi.fn(),
    testAutopilotPlatformHealth: vi.fn(),
    getAutopilotCurrentTask: vi.fn(),
  },
}))

const empty: AutopilotSnapshot = { session: null, providers: ['fake'] }
const templateEndpoint = (method: 'GET' | 'POST', path: string, body: unknown = null) => ({ method, path, query: {}, headers: {}, body, form: null, timeout_seconds: 30, max_retries: 0, retry_status_codes: [429, 500, 502, 503, 504] })
const defaultTemplatePlatform: StructuredPlatformInput = {
  base_url: '',
  auth: { type: 'bearer', token: '', header_name: 'Authorization', prefix: 'Bearer', parameter_name: 'token' },
  default_headers: { Accept: 'application/json' },
  fetch_task: templateEndpoint('POST', '/next'),
  current_task: null,
  task_info: null,
  submit_result: templateEndpoint('POST', '/attempt/{{external_id}}/submit', { flag: '{{submission.value}}' }),
  skip_task: null,
        mapping: { task_list_path: null, external_id_path: 'id', name_path: 'name', goal_path: 'goal', origin_path: 'origin', origin_endpoint_path: null, goal_format: 'plain', hints_path: 'hints', hints_item_path: null, attachments_path: null, cursor_path: null, lease_id_path: null },
  outcomes: { fetch: { finished: null, empty: null }, submit: { accepted: null, rejected: null, retryable: { status_codes: [429, 500, 502, 503, 504] } }, skip: { succeeded: null } },
  health_check: null,
  task_info_mode: 'always',
  task_info_failure_action: 'fail_task',
  task_info_delay_seconds: 0,
  initial_cursor: null,
}

describe('AutopilotPage', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(api.getAutopilot).mockResolvedValue(empty)
    vi.mocked(api.listAutopilotTemplates).mockResolvedValue([])
    vi.mocked(api.startAutopilot).mockResolvedValue({
      ...empty,
      session: {
        id: 'auto-1',
        status: 'running',
        mode: 'prompt',
        controller_provider: null,
        controller: { configured: true, base_url: 'https://controller.example/v1', model: 'controller-model' },
        solver_providers: ['fake'],
        available_providers: ['fake'],
        agent_limit: 1,
        max_tasks: 2,
        completed_tasks: 0,
        current_external_id: null,
        current_task_name: null,
        current_project_id: null,
        last_message: null,
        last_error: null,
        history: [],
      },
    })
    vi.mocked(api.getAutopilotCurrentTask).mockResolvedValue({
      ok: true,
      status: 'valid',
      category: 'platform_current_task',
      task: {
        external_id: 'attempt-1',
        name: 'Current challenge',
        goal: 'Recover the flag',
        origin: 'Challenge statement',
        hints: ['Inspect the response'],
        lease_id: 'lease-1',
      },
    })
  })

  it('keeps platform prompt in memory and submits it only on start', async () => {
    render(<AutopilotPage onBack={() => {}} onOpenProject={() => {}} />)

    await screen.findByText('连接配置')
    await userEvent.clear(screen.getByRole('textbox', { name: 'Base URL' }))
    await userEvent.type(screen.getByRole('textbox', { name: 'Base URL' }), 'https://controller.example/v1')
    await userEvent.type(screen.getByRole('textbox', { name: '模型' }), 'controller-model')
    await userEvent.type(screen.getByLabelText('API key'), 'controller-secret')
    await userEvent.type(screen.getByRole('textbox', { name: '平台说明 prompt' }), 'platform instructions')
    await userEvent.click(screen.getByRole('button', { name: '启动全自动' }))

    expect(api.startAutopilot).toHaveBeenCalledWith({
      mode: 'prompt',
      controller: {
        api_key: 'controller-secret',
        base_url: 'https://controller.example/v1',
        model: 'controller-model',
        advanced: {
          timeout_seconds: 300,
          max_rounds: 20,
          max_transport_retries: 2,
          temperature: null,
          max_output_tokens: null,
          reasoning_effort: null,
          extra_headers: {},
          extra_body: {},
        },
      },
      platform: { prompt: 'platform instructions' },
      solver_providers: ['fake'],
      agent_limit: 1,
      max_tasks: 10,
      task_timeout_minutes: 60,
      poll_interval_seconds: 1,
      rejected_action: 'fail',
      solver_failure_action: 'fail',
      stop_active_task_action: 'leave',
      max_replans_after_rejection: 1,
      max_transport_retries: 2,
    })
    await waitFor(() => {
      expect(screen.getByLabelText('API key')).toHaveValue('')
      expect(screen.getByRole('textbox', { name: '平台说明 prompt' })).toHaveValue('')
    })
  })

  it('queries and displays the configured current task without starting a session', async () => {
    render(<AutopilotPage onBack={() => {}} onOpenProject={() => {}} />)

    await screen.findByText('连接配置')
    await userEvent.click(screen.getByRole('button', { name: '结构化 API' }))
    await userEvent.type(screen.getByLabelText('平台 Base URL'), 'https://platform.example/api')
    const currentEndpoint = screen.getByRole('group', { name: '查看当前题目' })
    await userEvent.click(within(currentEndpoint).getByRole('checkbox', { name: '启用' }))
    await userEvent.type(within(currentEndpoint).getByLabelText('路径'), '/current')
    await userEvent.click(screen.getByRole('button', { name: '查看当前题目' }))

    expect(api.getAutopilotCurrentTask).toHaveBeenCalledWith(
      expect.objectContaining({
        base_url: 'https://platform.example/api',
        current_task: expect.objectContaining({ method: 'GET', path: '/current' }),
      }),
    )
    expect(await screen.findByText('Current challenge')).toBeInTheDocument()
    expect(screen.getByText('attempt-1')).toBeInTheDocument()
    expect(api.startAutopilot).not.toHaveBeenCalled()
  })

  it('saves and applies structured API templates without replacing the current token', async () => {
    const savedPlatform = {
      ...defaultTemplatePlatform,
      base_url: 'https://saved.example/api',
      auth: { ...defaultTemplatePlatform.auth, token: '' },
    }
    vi.mocked(api.listAutopilotTemplates).mockResolvedValue([{ id: 'template-1', name: 'Arena 1', created_at: 'now', updated_at: 'now', platform: savedPlatform }])
    vi.mocked(api.saveAutopilotTemplate).mockResolvedValue({ id: 'template-2', name: 'Arena 2', created_at: 'now', updated_at: 'now', platform: savedPlatform })

    render(<AutopilotPage onBack={() => {}} onOpenProject={() => {}} />)
    await screen.findByText('连接配置')
    await userEvent.click(screen.getByRole('button', { name: '结构化 API' }))
    await userEvent.type(screen.getByLabelText('平台 token'), 'current-secret')
    await userEvent.selectOptions(await screen.findByLabelText('结构化 API 模板'), 'template-1')
    await userEvent.click(screen.getByRole('button', { name: '套用' }))

    expect(screen.getByLabelText('平台 Base URL')).toHaveValue('https://saved.example/api')
    expect(screen.getByLabelText('平台 token')).toHaveValue('current-secret')

    await userEvent.type(screen.getByLabelText('新模板名称'), 'Arena 2')
    await userEvent.click(screen.getByRole('button', { name: '保存模板' }))
    expect(api.saveAutopilotTemplate).toHaveBeenCalledWith(expect.objectContaining({
      name: 'Arena 2',
      platform: expect.objectContaining({ base_url: 'https://saved.example/api', auth: expect.objectContaining({ token: '' }) }),
    }))
    expect(screen.getByLabelText('平台 token')).toHaveValue('current-secret')
  })

  it('defaults a configured rejection condition to false', async () => {
    render(<AutopilotPage onBack={() => {}} onOpenProject={() => {}} />)
    await screen.findByText('连接配置')
    await userEvent.click(screen.getByRole('button', { name: '结构化 API' }))
    await userEvent.type(screen.getByLabelText('拒绝结果路径'), 'data.correct')

    expect(screen.getByLabelText('拒绝结果值')).toHaveValue('false')
    await userEvent.selectOptions(screen.getByLabelText('拒绝结果值'), 'true')
    expect(screen.getByLabelText('拒绝结果值')).toHaveValue('true')
  })
})
