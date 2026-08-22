import type { BlackboardNode } from '../types'

const CJK_RANGES = [
  [0x3040, 0x30ff],
  [0x3400, 0x4dbf],
  [0x4e00, 0x9fff],
  [0xac00, 0xd7af],
  [0xf900, 0xfaff],
]

function isCjk(codePoint: number) {
  return CJK_RANGES.some(([start, end]) => codePoint >= start && codePoint <= end)
}

/** A stable UI estimate; the exact tokenizer varies by model/provider. */
export function estimateTokenCount(text: string) {
  let cjkCharacters = 0
  let otherCharacters = 0

  for (const character of text) {
    if (isCjk(character.codePointAt(0) ?? 0)) cjkCharacters += 1
    else if (!/\s/u.test(character)) otherCharacters += 1
  }

  return cjkCharacters + Math.ceil(otherCharacters / 4)
}

export function sumFactTokens(nodes: BlackboardNode[]) {
  return nodes.reduce(
    (total, node) => node.type === 'fact' ? total + estimateTokenCount(node.content) : total,
    0,
  )
}
