import { AlertTriangle, BrainCircuit, LoaderCircle, PauseCircle } from 'lucide-react'

import type { FactCompactionStatus as FactCompactionStatusValue } from '../types'

const labels: Record<string, string> = {
  disabled: '未启用',
  stopped: '未运行',
  waiting: '等待候选',
  compacting: '正在摘要',
  error: '摘要异常',
}

export function FactCompactionStatus({
  value,
}: {
  value?: FactCompactionStatusValue
}) {
  const status = value ?? {
    enabled: false,
    status: 'disabled',
    provider: null,
    model: null,
    pending_groups: 0,
    rejected_batches: 0,
    last_error: null,
  }
  const Icon = status.status === 'compacting'
    ? LoaderCircle
    : status.status === 'error'
      ? AlertTriangle
      : status.status === 'disabled' || status.status === 'stopped'
        ? PauseCircle
        : BrainCircuit

  return (
    <div className={`fact-compaction-status fact-compaction-status--${status.status}`} title={status.last_error ?? undefined}>
      <Icon size={15} className={status.status === 'compacting' ? 'fact-compaction-status__spin' : undefined} />
      <span className="fact-compaction-status__body">
        <strong>Fact 摘要 · {labels[status.status] ?? status.status}</strong>
        <small>
          {status.provider ?? '未配置 provider'}
          {status.model && ` · ${status.model}`}
          {status.enabled && ` · ${status.pending_groups} 个候选组`}
        </small>
      </span>
      {status.last_error && <span className="sr-only">{status.last_error}</span>}
    </div>
  )
}
