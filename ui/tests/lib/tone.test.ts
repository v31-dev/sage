import { describe, expect, it } from 'vitest'
import { TONES, type Tone } from '@/lib/tone'

const tones = Object.keys(TONES) as Tone[]
const kinds = ['text', 'chip', 'soft', 'dot', 'bar'] as const

// The theme token each meaning is drawn from (--success, --destructive, ...).
const token: Record<Tone, string> = {
  success: 'success',
  warning: 'warning',
  error: 'destructive',
  info: 'info',
  neutral: 'muted',
}

describe('TONES', () => {
  it('has the five meanings', () => {
    expect([...tones].sort()).toEqual(['error', 'info', 'neutral', 'success', 'warning'])
  })

  it.each(tones)('gives %s every kind of class', tone => {
    expect(Object.keys(TONES[tone]).sort()).toEqual([...kinds].sort())
    for (const kind of kinds) expect(TONES[tone][kind].length).toBeGreaterThan(0)
  })

  it.each(tones)('draws every class of %s from its own theme token', tone => {
    for (const kind of kinds) {
      expect(TONES[tone][kind], `${tone}.${kind}`).toContain(token[tone])
    }
  })

  it.each(tones.filter(tone => tone !== 'neutral'))(
    'keeps the classes of %s free of the other meanings',
    tone => {
      const others = tones.filter(other => other !== tone).map(other => token[other])
      for (const kind of kinds) {
        for (const other of others) {
          expect(TONES[tone][kind], `${tone}.${kind}`).not.toContain(other)
        }
      }
    }
  )
})
