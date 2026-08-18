import { Eye, EyeOff, PlugZap } from 'lucide-react'
import { useState } from 'react'

import type { ControllerInput, ValidationResult } from '../types'

export function AutopilotControllerFields({
  value,
  disabled,
  validation,
  onChange,
  onTest,
}: {
  value: ControllerInput
  disabled: boolean
  validation: ValidationResult | null
  onChange: (value: ControllerInput) => void
  onTest: () => void
}) {
  const [showKey, setShowKey] = useState(false)
  const advanced = value.advanced
  const update = (patch: Partial<ControllerInput>) => onChange({ ...value, ...patch })
  const updateAdvanced = (patch: Partial<ControllerInput['advanced']>) => {
    update({ advanced: { ...advanced, ...patch } })
  }

  return (
    <section className="auto-config-section">
      <div className="auto-section-heading">
        <h3>控制器 LLM</h3>
        <button className="secondary-button" type="button" onClick={onTest} disabled={disabled || !value.api_key || !value.base_url || !value.model}>
          <PlugZap size={15} /> 测试连接
        </button>
      </div>
      <div className="auto-fields-grid">
        <label className="auto-field auto-field--wide"><span>Base URL</span><input value={value.base_url} onChange={event => update({ base_url: event.target.value })} disabled={disabled} placeholder="https://api.example/v1" /></label>
        <label className="auto-field"><span>模型</span><input value={value.model} onChange={event => update({ model: event.target.value })} disabled={disabled} /></label>
        <label className="auto-field auto-field--wide"><span>API key</span><div className="secret-input"><input type={showKey ? 'text' : 'password'} value={value.api_key} onChange={event => update({ api_key: event.target.value })} disabled={disabled} autoComplete="off" /><button className="icon-button" type="button" aria-label={showKey ? '隐藏 API key' : '显示 API key'} title={showKey ? '隐藏 API key' : '显示 API key'} onClick={() => setShowKey(current => !current)}>{showKey ? <EyeOff size={16} /> : <Eye size={16} />}</button></div></label>
      </div>
      {validation && <p className={`validation-state validation-state--${validation.status}`}>{validation.status === 'valid' ? `连接正常 · ${validation.model ?? value.model}` : validation.status}</p>}
      <details className="auto-advanced">
        <summary>高级选项</summary>
        <div className="auto-fields-grid">
          <label className="auto-field"><span>超时（秒）</span><input type="number" min={1} max={1800} value={advanced.timeout_seconds} onChange={event => updateAdvanced({ timeout_seconds: Number(event.target.value) })} disabled={disabled} /></label>
          <label className="auto-field"><span>最大工具轮次</span><input type="number" min={1} max={50} value={advanced.max_rounds} onChange={event => updateAdvanced({ max_rounds: Number(event.target.value) })} disabled={disabled} /></label>
          <label className="auto-field"><span>Temperature</span><input type="number" min={0} max={2} step={0.1} value={advanced.temperature ?? ''} onChange={event => updateAdvanced({ temperature: event.target.value === '' ? null : Number(event.target.value) })} disabled={disabled} /></label>
          <label className="auto-field"><span>最大输出 tokens</span><input type="number" min={1} value={advanced.max_output_tokens ?? ''} onChange={event => updateAdvanced({ max_output_tokens: event.target.value === '' ? null : Number(event.target.value) })} disabled={disabled} /></label>
        </div>
      </details>
    </section>
  )
}
