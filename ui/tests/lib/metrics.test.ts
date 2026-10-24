import { describe, expect, it } from 'vitest'
import { processContainerData } from '@/lib/metrics'
import { colors } from '@/lib/utils'
import type { ContainerMetricsPoint } from '@/services/api'

function point(overrides: Partial<ContainerMetricsPoint> = {}): ContainerMetricsPoint {
  return {
    ts: '2026-10-24T10:00:00Z',
    name: 'web',
    cpu_pct: 12.5,
    mem_used_mb: 128,
    net_rx_kbps: 1234,
    net_tx_kbps: 56,
    ...overrides,
  }
}

describe('processContainerData', () => {
  it('gives nothing for no containers', () => {
    expect(processContainerData([])).toEqual({ data: [], memMax: 0, netMax: 0, colors: [] })
  })

  it('makes one record per timestamp with a value and a label per measure', () => {
    const { data } = processContainerData([[point()]])

    expect(data).toHaveLength(1)
    expect(data[0]).toEqual({
      date: new Date('2026-10-24T10:00:00Z'),
      web_cpu_pct: 12.5,
      web_cpu_pct_label: '12.5%',
      web_mem_used_mb: 128,
      web_mem_used_mb_label: '128 MB',
      web_net_rx: 1.2,
      web_net_rx_label: '1.2 Mbps',
      web_net_tx: 0.1,
      web_net_tx_label: '0.1 Mbps',
    })
  })

  it.each([
    [1250, 1.3],
    [1249, 1.2],
    [0, 0],
    [50, 0.1],
    [49, 0],
    [999000, 999],
  ])('turns %s kbps into %s Mbps, rounded to one decimal', (kbps, mbps) => {
    const { data } = processContainerData([[point({ net_rx_kbps: kbps })]])
    expect(data[0]!.web_net_rx).toBe(mbps)
  })

  it('shows N/A and keeps null for a measure that is missing', () => {
    const { data } = processContainerData([
      [point({ cpu_pct: null, mem_used_mb: null, net_rx_kbps: null, net_tx_kbps: null })],
    ])

    expect(data[0]).toMatchObject({
      web_cpu_pct: null,
      web_cpu_pct_label: 'N/A',
      web_mem_used_mb: null,
      web_mem_used_mb_label: 'N/A',
      web_net_rx: null,
      web_net_rx_label: 'N/A',
      web_net_tx: null,
      web_net_tx_label: 'N/A',
    })
  })

  it('shows 0 as a value, not as N/A', () => {
    const { data } = processContainerData([[point({ cpu_pct: 0, mem_used_mb: 0 })]])

    expect(data[0]).toMatchObject({ web_cpu_pct_label: '0%', web_mem_used_mb_label: '0 MB' })
  })

  it('puts containers that share a timestamp in one record', () => {
    const { data } = processContainerData([
      [point({ name: 'web', cpu_pct: 10 })],
      [point({ name: 'db', cpu_pct: 20 })],
    ])

    expect(data).toHaveLength(1)
    expect(data[0]).toMatchObject({ web_cpu_pct: 10, db_cpu_pct: 20 })
  })

  it('keeps separate records for different timestamps in the order they first appear', () => {
    const { data } = processContainerData([
      [point({ ts: '2026-10-24T10:00:00Z' }), point({ ts: '2026-10-24T10:01:00Z' })],
      [point({ name: 'db', ts: '2026-10-24T09:59:00Z' })],
    ])

    expect(data.map(record => record.date.toISOString())).toEqual([
      '2026-10-24T10:00:00.000Z',
      '2026-10-24T10:01:00.000Z',
      '2026-10-24T09:59:00.000Z',
    ])
  })

  it('reads a timestamp without a timezone the way the browser does', () => {
    const { data } = processContainerData([[point({ ts: '2026-10-24T10:00:00' })]])
    expect(data[0]!.date).toEqual(new Date('2026-10-24T10:00:00'))
  })

  it('tracks the highest memory and network figure over all containers and timestamps', () => {
    const { memMax, netMax } = processContainerData([
      [
        point({ mem_used_mb: 100, net_rx_kbps: 2000, net_tx_kbps: 500 }),
        point({
          ts: '2026-10-24T10:01:00Z',
          mem_used_mb: 300,
          net_rx_kbps: 100,
          net_tx_kbps: 9000,
        }),
      ],
      [point({ name: 'db', mem_used_mb: 200, net_rx_kbps: 4000, net_tx_kbps: 100 })],
    ])

    expect(memMax).toBe(300)
    expect(netMax).toBe(9)
  })

  it.each([
    ['receive', 5000, 100],
    ['send', 100, 5000],
  ])('takes the network maximum from %s when that is the higher direction', (_name, rx, tx) => {
    const { netMax } = processContainerData([[point({ net_rx_kbps: rx, net_tx_kbps: tx })]])
    expect(netMax).toBe(5)
  })

  it('ignores missing figures in the maxima', () => {
    const { memMax, netMax } = processContainerData([
      [point({ mem_used_mb: null, net_rx_kbps: null, net_tx_kbps: null })],
    ])

    expect([memMax, netMax]).toEqual([0, 0])
  })

  it('gives each container a color in the order the containers come, from the shared palette', () => {
    const { colors: assigned } = processContainerData([
      [point({ name: 'web' })],
      [point({ name: 'db' })],
      [point({ name: 'cache' })],
    ])

    expect(assigned).toEqual([
      { name: 'web', color: colors[0] },
      { name: 'db', color: colors[1] },
      { name: 'cache', color: colors[2] },
    ])
  })

  it('starts the palette again after the twentieth container', () => {
    const containers = Array.from({ length: 21 }, (_, index) => [point({ name: `c${index}` })])
    const { colors: assigned } = processContainerData(containers)

    expect(assigned).toHaveLength(21)
    expect(assigned[20]).toEqual({ name: 'c20', color: colors[0] })
  })

  it('names a container once however many points it has', () => {
    const { colors: assigned } = processContainerData([
      [point(), point({ ts: '2026-10-24T10:01:00Z' }), point({ ts: '2026-10-24T10:02:00Z' })],
    ])

    expect(assigned).toHaveLength(1)
  })

  it('skips a container without points or whose first point has no name', () => {
    const { colors: assigned, data } = processContainerData([[], [point({ name: undefined })]])

    expect(assigned).toEqual([])
    expect(data).toHaveLength(1)
  })
})
