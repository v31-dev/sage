<script setup lang="ts">
import LogViewer from '@/components/LogViewer.vue';


function parseMessage(raw: string, entry: any): { [key: string]: any; ts: string; message: string } {
  try {
    const m = JSON.parse(raw)

    return {
      ts: m['StartUTC'] ?? '',
      worker: entry['hostname'] ?? '',
      client: m['ClientHost'] ?? '',
      message: `${m['RequestMethod']} ${m['RequestHost']}${m['RequestPath']} ${m['DownstreamStatus']} ${m['DownstreamContentSize']}B ${(parseInt(m['Duration'])/10e6).toFixed(2)}s`
    };
  } catch {
    return { ts: '', message: raw };
  }
}

const _tsFormatter = new Intl.DateTimeFormat(undefined, {
  month: 'short', day: '2-digit',
  hour: '2-digit', minute: '2-digit', second: '2-digit',
  hour12: true,
})

// dateString like '2026-03-19T16:33:22.427659841Z'
function formatTs(dateString: string): string {
  try {
    return _tsFormatter.format(new Date(dateString)).replace(',', '')
  } catch {
    return dateString
  }
}

const columns = [
  {
    key: 'ts',
    label: 'Timestamp',
    headerClass: 'pl-4 py-2 text-xs w-40',
    rowClass: "pl-4 py-2 text-xs w-40 font-mono text-muted-foreground whitespace-nowrap",
    formatter: formatTs
  },
  {
    key: 'worker',
    label: 'Worker',
    headerClass: 'py-2 text-xs w-24',
    rowClass: "py-2 text-xs w-24 font-mono whitespace-nowrap"
  },
  {
    key: 'client',
    label: 'Client',
    headerClass: 'py-2 text-xs w-32',
    rowClass: "py-2 text-xs w-32 font-mono whitespace-nowrap"
  },
  {
    key: 'message',
    label: 'Message',
    headerClass: 'py-2 text-xs',
    rowClass: "py-2 text-xs font-mono break-all whitespace-pre-wrap"
  },
]
</script>

<template>
  <LogViewer hostname="*" container="traefik" :parseMessage="parseMessage" :columns="columns" />
</template>