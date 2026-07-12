import { Plus, X } from 'lucide-react'
import { FormEvent, useEffect, useState } from 'react'

export interface NewProjectPayload {
  name: string
  goal: string
  origin: string
  hints: string[]
  mode: 'manual' | 'run'
  providers: string[]
  agent_limit: number
  challenge_path: string | null
}

export function NewProjectDialog({
  open,
  providers,
  onSubmit,
  onClose,
}: {
  open: boolean
  providers: string[]
  onSubmit: (payload: NewProjectPayload) => void | Promise<void>
  onClose: () => void
}) {
  const [mode, setMode] = useState<'manual' | 'run'>('manual')
  const [selected, setSelected] = useState<string[]>([])
  const [hints, setHints] = useState<string[]>([''])

  useEffect(() => {
    if (!open) {
      setMode('manual')
      setSelected([])
      setHints([''])
    }
  }, [open])

  if (!open) return null

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const data = new FormData(event.currentTarget)
    void onSubmit({
      name: String(data.get('name')).trim(),
      goal: String(data.get('goal')).trim(),
      origin: String(data.get('origin')).trim(),
      hints: hints.map(item => item.trim()).filter(Boolean),
      mode,
      providers: selected,
      agent_limit: Number(data.get('agent_limit')),
      challenge_path: String(data.get('challenge_path')).trim() || null,
    })
  }

  return (
    <div className="dialog-backdrop" role="presentation">
      <form className="project-dialog" role="dialog" aria-modal="true" onSubmit={submit}>
        <header className="dialog-header">
          <div><h2>新建项目</h2><p>一个项目对应一个隔离黑板和任务。</p></div>
          <button type="button" className="icon-button" onClick={onClose} aria-label="关闭">
            <X size={20} />
          </button>
        </header>
        <div className="dialog-grid">
          <label>项目名称<input name="name" required /></label>
          <label>Goal<textarea name="goal" required rows={3} /></label>
          <label>Origin<textarea name="origin" required rows={4} /></label>
          <fieldset>
            <legend>启动模式</legend>
            <label className="choice-row"><input type="radio" name="mode" checked={mode === 'manual'} onChange={() => setMode('manual')} />仅黑板</label>
            <label className="choice-row"><input type="radio" name="mode" checked={mode === 'run'} onChange={() => setMode('run')} />立即启动 Agent</label>
          </fieldset>
          <fieldset>
            <legend>Agent</legend>
            {providers.map(provider => (
              <label className="choice-row" key={provider}>
                <input
                  type="checkbox"
                  checked={selected.includes(provider)}
                  onChange={event => setSelected(current => event.target.checked ? [...current, provider] : current.filter(item => item !== provider))}
                />
                {provider}
              </label>
            ))}
          </fieldset>
          <label>并发 Agent 数<input name="agent_limit" type="number" min="1" max="16" defaultValue="2" /></label>
          <label>Challenge 目录<input name="challenge_path" placeholder="可选，本地只读目录" /></label>
          <div className="hints-editor">
            <span>初始 Hint</span>
            {hints.map((hint, index) => (
              <input
                key={index}
                aria-label={`Hint ${index + 1}`}
                value={hint}
                onChange={event => setHints(current => current.map((item, itemIndex) => itemIndex === index ? event.target.value : item))}
              />
            ))}
            <button type="button" className="text-button" onClick={() => setHints(current => [...current, ''])}>
              <Plus size={15} /> 添加 Hint
            </button>
          </div>
        </div>
        <footer className="dialog-actions">
          <button type="button" className="secondary-button" onClick={onClose}>取消</button>
          <button type="submit" className="primary-button">{mode === 'run' ? '创建并启动' : '创建项目'}</button>
        </footer>
      </form>
    </div>
  )
}
