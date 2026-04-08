import { colors } from './utils';
import { type ContainerMetricsPoint } from '@/services/api';

export function processContainerData(containers: Array<Array<ContainerMetricsPoint>>) {
  const data = [] as Record<string, any>[];
  const names = new Set<string>();

  for (const containerData of containers) {
    if (containerData.length > 0 && containerData[0] && containerData[0].name) {
      names.add(containerData[0].name);
    }
    for (const point of containerData) {
      const name = point.name;
      const date = new Date(point.ts);
      const cpu = point.cpu_pct;
      const mem = point.mem_used_mb;
      const net_rx_mbps =
        point.net_rx_kbps !== null ? Math.round((point.net_rx_kbps / 1000) * 10) / 10 : null;
      const net_tx_mbps =
        point.net_tx_kbps !== null ? Math.round((point.net_tx_kbps / 1000) * 10) / 10 : null;

      const pointData = {
        [`${name}_cpu_pct`]: cpu,
        [`${name}_cpu_pct_label`]: cpu !== null ? `${cpu}%` : 'N/A',
        [`${name}_mem_used_mb`]: mem,
        [`${name}_mem_used_mb_label`]: mem !== null ? `${mem} MB` : 'N/A',
        [`${name}_net_rx`]: net_rx_mbps,
        [`${name}_net_rx_label`]: net_rx_mbps !== null ? `${net_rx_mbps} Mbps` : 'N/A',
        [`${name}_net_tx`]: net_tx_mbps,
        [`${name}_net_tx_label`]: net_tx_mbps !== null ? `${net_tx_mbps} Mbps` : 'N/A',
      };

      const record = data.find((r: any) => r.date.getTime() === date.getTime());

      if (record) {
        for (const [key, value] of Object.entries(pointData)) {
          record[key] = value;
        }
      } else {
        data.push({ date, ...pointData });
      }
    }
  }

  const memMax = Math.max(
    0,
    ...data.map(point => {
      let max = 0;
      for (const key in point) {
        if (key.endsWith('_mem_used_mb')) {
          max = Math.max(max, point[key] || 0);
        }
      }
      return max;
    })
  );

  const netMax = Math.max(
    0,
    ...data.map(point => {
      let max = 0;
      for (const key in point) {
        if (key.endsWith('_net_rx') || key.endsWith('_net_tx')) {
          max = Math.max(max, point[key] || 0);
        }
      }
      return max;
    })
  );

  const containerColors = Array.from(names).map((name, index) => {
    const color = colors[index % colors.length]!;
    return { name, color };
  });

  return { data, memMax, netMax, colors: containerColors };
}
