import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { TONES } from '@/lib/tone'
import {
  cn,
  colors,
  formatDate,
  formatDateStringAgo,
  levelClass,
  parseDate,
  startDownload,
} from '@/lib/utils'

describe('cn', () => {
  it('joins class names and drops the empty ones', () => {
    expect(cn('a', false, null, undefined, '', 'b')).toBe('a b')
  })

  it('takes arrays and objects', () => {
    expect(cn(['a', 'b'], { c: true, d: false })).toBe('a b c')
  })

  it('lets the later Tailwind class win over a conflicting earlier one', () => {
    expect(cn('p-2', 'p-4')).toBe('p-4')
    expect(cn('text-red-500', 'text-blue-500')).toBe('text-blue-500')
    expect(cn('px-2 py-1', 'p-3')).toBe('p-3')
  })

  it('keeps classes that do not conflict', () => {
    expect(cn('p-2', 'm-2')).toBe('p-2 m-2')
  })
})

describe('colors', () => {
  it('has twenty different hex colors', () => {
    expect(colors).toHaveLength(20)
    expect(new Set(colors).size).toBe(20)
    expect(colors.every(color => /^#[0-9a-f]{6}$/.test(color))).toBe(true)
  })
})

describe('parseDate', () => {
  it.each([
    ['without a timezone, which is UTC', '2027-01-13T09:10:59', '2027-01-13T09:10:59.000Z'],
    ['with Z', '2027-01-13T09:10:59Z', '2027-01-13T09:10:59.000Z'],
    ['with a lowercase z', '2027-01-13T09:10:59z', '2027-01-13T09:10:59.000Z'],
    ['with +00:00', '2027-01-13T09:10:59+00:00', '2027-01-13T09:10:59.000Z'],
    ['with a positive offset', '2027-01-13T09:10:59+05:30', '2027-01-13T03:40:59.000Z'],
    ['with a negative offset', '2027-01-13T09:10:59-08:00', '2027-01-13T17:10:59.000Z'],
    ['with an offset without a colon', '2027-01-13T09:10:59+0530', '2027-01-13T03:40:59.000Z'],
    ['with microseconds and no timezone', '2026-10-24T10:05:07.123456', '2026-10-24T10:05:07.123Z'],
    ['with nanoseconds and Z', '2026-03-19T16:12:42.759325367Z', '2026-03-19T16:12:42.759Z'],
  ])('reads a string %s', (_name, value, iso) => {
    expect(parseDate(value).toISOString()).toBe(iso)
  })

  it('gives a Date back as it is', () => {
    const date = new Date('2027-01-13T09:10:59Z')
    expect(parseDate(date)).toBe(date)
  })

  it('gives an invalid Date for text that is not a date', () => {
    expect(Number.isNaN(parseDate('garbage').getTime())).toBe(true)
  })
})

describe('formatDate', () => {
  it.each([
    ['2026-10-24T10:05:07Z', 'Oct 24 10:05:07 AM'],
    ['2026-10-24T15:04:05Z', 'Oct 24 03:04:05 PM'],
    ['2026-01-02T00:00:00Z', 'Jan 02 12:00:00 AM'],
    ['2026-01-02T12:00:00Z', 'Jan 02 12:00:00 PM'],
  ])('shows %s as %s', (value, shown) => {
    expect(formatDate(value)).toBe(shown)
  })

  it('reads a string without a timezone as UTC', () => {
    expect(formatDate('2026-10-24T10:05:07')).toBe('Oct 24 10:05:07 AM')
  })

  it('formats a Date as it is', () => {
    expect(formatDate(new Date('2026-10-24T10:05:07Z'))).toBe('Oct 24 10:05:07 AM')
  })

  it('shows the text itself when it is not a date', () => {
    expect(formatDate('garbage')).toBe('garbage')
  })

  it('does not throw for an invalid Date either', () => {
    expect(formatDate(new Date('garbage'))).toBe('Invalid Date')
  })
})

describe('levelClass', () => {
  it.each([
    ['ERROR', `${TONES.error.text} font-semibold`],
    ['error', `${TONES.error.text} font-semibold`],
    ['CRITICAL', `${TONES.error.text} font-semibold`],
    ['CRIT', `${TONES.error.text} font-semibold`],
    ['WARNING', `${TONES.warning.text} font-semibold`],
    ['warn', `${TONES.warning.text} font-semibold`],
    ['DEBUG', TONES.neutral.text],
    ['INFO', TONES.info.text],
    ['TRACE', TONES.info.text],
    ['', TONES.info.text],
  ])('gives %s its class', (level, classes) => {
    expect(levelClass(level)).toBe(classes)
  })
})

describe('formatDateStringAgo', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-10-24T12:00:00Z'))
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  const ago = (seconds: number) => new Date(Date.now() - seconds * 1000).toISOString()

  it.each([
    ['30 seconds', 30, 'just now (Oct 24 11:59:30 AM)'],
    ['59 seconds', 59, 'just now (Oct 24 11:59:01 AM)'],
    ['a minute', 60, '1m ago (Oct 24 11:59:00 AM)'],
    ['59 minutes', 59 * 60, '59m ago (Oct 24 11:01:00 AM)'],
    ['an hour', 3600, '1h ago (Oct 24 11:00:00 AM)'],
    ['23 hours', 23 * 3600 + 59 * 60, '23h ago (Oct 23 12:01:00 PM)'],
    ['a day', 86400, '1d ago (Oct 23 12:00:00 PM)'],
    ['6 days', 6 * 86400 + 23 * 3600, '6d ago (Oct 17 01:00:00 PM)'],
  ])('says how long ago for %s', (_name, seconds, text) => {
    expect(formatDateStringAgo(ago(seconds))).toBe(text)
  })

  it('shows only the date from a week on', () => {
    expect(formatDateStringAgo(ago(7 * 86400))).toBe('Oct 17 12:00:00 PM')
    expect(formatDateStringAgo(ago(30 * 86400))).toBe('Sep 24 12:00:00 PM')
  })

  it('says just now for a time in the future', () => {
    expect(formatDateStringAgo(ago(-300))).toBe('just now (Oct 24 12:05:00 PM)')
  })

  it('reads a string without a timezone as UTC and takes a Date', () => {
    expect(formatDateStringAgo('2026-10-24T11:55:00')).toBe('5m ago (Oct 24 11:55:00 AM)')
    expect(formatDateStringAgo(new Date('2026-10-24T11:55:00Z'))).toBe(
      '5m ago (Oct 24 11:55:00 AM)'
    )
  })

  it('does not throw for text that is not a date', () => {
    expect(formatDateStringAgo('garbage')).toBe('Invalid Date')
  })
})

describe('startDownload', () => {
  it('clicks a link to the address that opens in a new tab and keeps the opener out', () => {
    const clicked: HTMLAnchorElement[] = []
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (
      this: HTMLAnchorElement
    ) {
      clicked.push(this)
    })

    startDownload('https://s3.example.test/backup.enc?sig=abc')

    expect(clicked).toHaveLength(1)
    expect(clicked[0]!.href).toBe('https://s3.example.test/backup.enc?sig=abc')
    expect(clicked[0]!.target).toBe('_blank')
    expect(clicked[0]!.rel).toBe('noopener')
  })

  it('does not leave the link in the page', () => {
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
    const before = document.body.innerHTML

    startDownload('https://s3.example.test/x')

    expect(document.body.innerHTML).toBe(before)
  })
})

describe('parseDate on a machine that is not on UTC', () => {
  beforeEach(() => {
    process.env.TZ = 'Asia/Kolkata'
  })

  afterEach(() => {
    process.env.TZ = 'UTC'
  })

  it('still reads a string without a timezone as UTC', () => {
    expect(parseDate('2027-01-13T09:10:59').toISOString()).toBe('2027-01-13T09:10:59.000Z')
    expect(parseDate('2026-10-24T10:05:07.123456').toISOString()).toBe('2026-10-24T10:05:07.123Z')
  })

  it('reads a string with a timezone as given', () => {
    expect(parseDate('2027-01-13T09:10:59Z').toISOString()).toBe('2027-01-13T09:10:59.000Z')
    expect(parseDate('2027-01-13T09:10:59+05:30').toISOString()).toBe('2027-01-13T03:40:59.000Z')
  })
})
