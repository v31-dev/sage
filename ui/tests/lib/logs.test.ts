import { describe, expect, it } from 'vitest'
import { parseManagerLog } from '@/lib/logs'

describe('parseManagerLog', () => {
  it('splits a line in the manager log format into its parts', () => {
    const entry = parseManagerLog(
      '[2026-10-24 10:05:07,123] [INFO    ] [services.manager ] [a1b2c3d4] Deployed demo-web'
    )

    expect(entry).toEqual({
      ts: '2026-10-24 10:05:07',
      level: 'INFO',
      logger: 'services.manager',
      taskId: 'a1b2c3d4',
      message: 'Deployed demo-web',
    })
  })

  it('reads the line the entrypoint prints before the manager starts', () => {
    const entry = parseManagerLog(
      '[2026-10-24 10:00:00,001] [CRITICAL] [entrypoint         ] [] Could not join the tailnet'
    )

    expect(entry.level).toBe('CRITICAL')
    expect(entry.logger).toBe('entrypoint')
    expect(entry.taskId).toBe('')
    expect(entry.message).toBe('Could not join the tailnet')
  })

  it('keeps every line of a message that runs over several lines', () => {
    const entry = parseManagerLog(
      '[2026-10-24 10:05:07,123] [ERROR   ] [utils.queue] [t1] Task failed\nTraceback (most recent call last):\n  File "x.py", line 1'
    )

    expect(entry.message).toBe(
      'Task failed\nTraceback (most recent call last):\n  File "x.py", line 1'
    )
  })

  it('drops the surrounding blanks of every part', () => {
    const entry = parseManagerLog(
      '[ 2026-10-24 10:05:07,123 ]  [ WARNING ]  [ a ]  [ t ]   spaced   '
    )

    expect([entry.ts, entry.level, entry.logger, entry.taskId, entry.message]).toEqual([
      '2026-10-24 10:05:07',
      'WARNING',
      'a',
      't',
      'spaced',
    ])
  })

  it('has an empty message and empty parts for an empty message', () => {
    const entry = parseManagerLog('[2026-10-24 10:05:07,123] [INFO] [a] [t]')
    expect(entry.message).toBe('')
    expect(entry.level).toBe('INFO')
  })

  it.each([
    ['plain text', 'Starting up'],
    ['three brackets only', '[a] [b] [c] text'],
    ['an empty string', ''],
    ['a line with the brackets in another order', 'INFO [a] [b] [c] text'],
  ])('returns %s whole as the message, with empty parts', (_name, raw) => {
    expect(parseManagerLog(raw)).toEqual({
      ts: '',
      level: '',
      logger: '',
      taskId: '',
      message: raw,
    })
  })
})
