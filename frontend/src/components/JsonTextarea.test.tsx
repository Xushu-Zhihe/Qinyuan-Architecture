import { fireEvent, render, screen } from '@testing-library/react'
import { useState } from 'react'
import { describe, expect, it } from 'vitest'

import { JsonTextarea } from './JsonTextarea'

function ObjectHarness() {
  const [value, setValue] = useState<Record<string, string>>({})
  return (
    <JsonTextarea
      label="额外 headers JSON"
      value={value}
      emptyValue={{}}
      objectOnly
      onChange={setValue}
    />
  )
}

describe('JsonTextarea', () => {
  it('keeps an invalid editing draft and commits it once it becomes valid', () => {
    render(<ObjectHarness />)
    const editor = screen.getByLabelText('额外 headers JSON')

    fireEvent.change(editor, { target: { value: '{' } })
    expect(editor).toHaveValue('{')
    expect(screen.getByRole('alert')).toHaveTextContent('JSON 格式无效')

    fireEvent.change(editor, { target: { value: '{"X-Test":"enabled"}' } })
    expect(editor).toHaveValue('{"X-Test":"enabled"}')
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('allows clearing before entering a replacement object', () => {
    render(<ObjectHarness />)
    const editor = screen.getByLabelText('额外 headers JSON')

    fireEvent.change(editor, { target: { value: '' } })
    expect(editor).toHaveValue('')

    fireEvent.change(editor, { target: { value: '{"Authorization":"token"}' } })
    expect(editor).toHaveValue('{"Authorization":"token"}')
  })

  it('rejects valid JSON values that are not objects when objectOnly is enabled', () => {
    render(<ObjectHarness />)
    const editor = screen.getByLabelText('额外 headers JSON')

    fireEvent.change(editor, { target: { value: '[]' } })
    expect(editor).toHaveValue('[]')
    expect(screen.getByRole('alert')).toHaveTextContent('必须输入 JSON 对象')
  })
})
