import { useEffect, useId, useRef, useState } from 'react'

function serialize(value: unknown) {
  return JSON.stringify(value, null, 2) ?? ''
}

export function JsonTextarea<T>({
  label,
  value,
  emptyValue,
  objectOnly = false,
  disabled = false,
  rows = 3,
  className = '',
  onChange,
}: {
  label: string
  value: T
  emptyValue: T
  objectOnly?: boolean
  disabled?: boolean
  rows?: number
  className?: string
  onChange: (value: T) => void
}) {
  const serializedValue = serialize(value)
  const lastValue = useRef(serializedValue)
  const [draft, setDraft] = useState(serializedValue)
  const [error, setError] = useState<string | null>(null)
  const errorId = useId()

  useEffect(() => {
    if (serializedValue === lastValue.current) return
    lastValue.current = serializedValue
    setDraft(serializedValue)
    setError(null)
  }, [serializedValue])

  const commit = (nextValue: T) => {
    lastValue.current = serialize(nextValue)
    onChange(nextValue)
  }

  const updateDraft = (raw: string) => {
    setDraft(raw)
    if (!raw.trim()) {
      setError(null)
      commit(emptyValue)
      return
    }
    try {
      const parsed = JSON.parse(raw)
      if (objectOnly && (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed))) {
        setError('必须输入 JSON 对象')
        return
      }
      setError(null)
      commit(parsed as T)
    } catch {
      setError('JSON 格式无效')
    }
  }

  return (
    <label className={`auto-field ${className}`.trim()}>
      <span>{label}</span>
      <textarea
        rows={rows}
        value={draft}
        onChange={event => updateDraft(event.target.value)}
        disabled={disabled}
        aria-invalid={error ? true : undefined}
        aria-describedby={error ? errorId : undefined}
      />
      {error && <small id={errorId} className="json-field-error" role="alert">{error}</small>}
    </label>
  )
}
