import { colors } from './utils'
import { type ContainerMetricsPoint } from '@/services/api'

export function processContainerData(containers: Array<Array<ContainerMetricsPoint>>) {
  const recordsByTimestamp = new Map<number, Record<string, any>>()
  const names = new Set<string>()
  let memMax = 0
  let netMax = 0

  for (const containerData of containers) {
    if (containerData.length > 0 && containerData[0] && containerData[0].name) {
      names.add(containerData[0].name)
    }
    for (const point of containerData) {
      const name = point.name
      const date = new Date(point.ts)
      const cpu = point.cpu_pct
      const mem = point.mem_used_mb
      const net_rx_mbps =
        point.net_rx_kbps !== null ? Math.round((point.net_rx_kbps / 1000) * 10) / 10 : null
      const net_tx_mbps =
        point.net_tx_kbps !== null ? Math.round((point.net_tx_kbps / 1000) * 10) / 10 : null

      const timestamp = date.getTime()
      let record = recordsByTimestamp.get(timestamp)

      if (!record) {
        record = { date }
        recordsByTimestamp.set(timestamp, record)
      }

      record[`${name}_cpu_pct`] = cpu
      record[`${name}_cpu_pct_label`] = cpu !== null ? `${cpu}%` : 'N/A'
      record[`${name}_mem_used_mb`] = mem
      record[`${name}_mem_used_mb_label`] = mem !== null ? `${mem} MB` : 'N/A'
      record[`${name}_net_rx`] = net_rx_mbps
      record[`${name}_net_rx_label`] = net_rx_mbps !== null ? `${net_rx_mbps} Mbps` : 'N/A'
      record[`${name}_net_tx`] = net_tx_mbps
      record[`${name}_net_tx_label`] = net_tx_mbps !== null ? `${net_tx_mbps} Mbps` : 'N/A'

      memMax = Math.max(memMax, mem ?? 0)
      netMax = Math.max(netMax, net_rx_mbps ?? 0, net_tx_mbps ?? 0)
    }
  }

  const data = Array.from(recordsByTimestamp.values())

  const containerColors = Array.from(names).map((name, index) => {
    const color = colors[index % colors.length]!
    return { name, color }
  })

  return { data, memMax, netMax, colors: containerColors }
}
