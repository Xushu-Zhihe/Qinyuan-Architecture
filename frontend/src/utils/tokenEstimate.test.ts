import { describe, expect, it } from 'vitest'

import type { BlackboardNode } from '../types'
import { estimateTokenCount, sumFactTokens } from './tokenEstimate'

describe('fact token estimates', () => {
  it('counts CJK characters individually and latin text approximately', () => {
    expect(estimateTokenCount('事实')).toBe(2)
    expect(estimateTokenCount('abcd')).toBe(1)
    expect(estimateTokenCount('abcdef')).toBe(2)
  })

  it('ignores whitespace and handles empty content', () => {
    expect(estimateTokenCount('  \n\t')).toBe(0)
    expect(estimateTokenCount('')).toBe(0)
  })

  it('sums only Fact nodes', () => {
    const nodes = [
      { type: 'fact', content: '事实' },
      { type: 'goal', content: 'abcd' },
      { type: 'fact', content: 'abcd' },
    ] as BlackboardNode[]
    expect(sumFactTokens(nodes)).toBe(3)
  })
})
