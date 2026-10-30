<script setup lang="ts">
import { nextTick, onMounted, onUnmounted, ref } from 'vue'
import { Card, CardContent } from '@/components/ui/card'
import { Spinner } from '@/components/ui/spinner'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import { formatDate, levelClass } from '@/lib/utils'
import { fetchTaskLogs, type TaskLogLine } from '@/services/api'

// A task's lines, or one container's part of it, loaded again every 2 s while shown.
const props = defineProps<{ taskId: string; subTaskId?: string }>()

const POLL_INTERVAL = 2_000

const lines = ref<TaskLogLine[]>([])
const more = ref(false)
const loaded = ref(false)
const error = ref('')
const viewport = ref<HTMLElement | null>(null)
let timer: ReturnType<typeof setTimeout> | undefined
let stopped = false

function atBottom() {
  const view = viewport.value
  return !view || view.scrollHeight - (view.scrollTop + view.clientHeight) <= 50
}

// One chain: the next load is timed from the end of this one. The view follows new lines while it is at the bottom.
async function load() {
  const follow = atBottom()
  try {
    const logs = await fetchTaskLogs(props.taskId, props.subTaskId)
    lines.value = logs.lines
    more.value = logs.more
    error.value = ''
  } catch (err) {
    error.value = err instanceof Error ? err.message : 'Failed to load the logs'
  } finally {
    loaded.value = true
  }
  if (stopped) return
  if (follow && viewport.value) {
    await nextTick()
    viewport.value.scrollTop = viewport.value.scrollHeight
  }
  timer = setTimeout(load, POLL_INTERVAL)
}

onMounted(load)
onUnmounted(() => {
  stopped = true
  clearTimeout(timer)
})
</script>

<template>
  <div class="flex flex-col min-h-0 gap-2">
    <p v-if="error" class="text-sm text-destructive">{{ error }}</p>
    <p v-if="more" class="text-xs text-muted-foreground">
      Showing the newest {{ lines.length }} lines.
    </p>
    <div v-if="!loaded" class="flex justify-center py-12">
      <Spinner class="animate-spin" />
    </div>
    <Card v-else-if="!lines.length">
      <CardContent class="flex items-center justify-center py-12">
        <p class="text-muted-foreground">No logs found</p>
      </CardContent>
    </Card>
    <div v-else ref="viewport" class="min-h-0 overflow-y-auto md:border md:rounded-lg md:bg-card">
      <!-- Desktop table (md+) -->
      <Table class="hidden md:table table-fixed">
        <TableHeader class="sticky top-0 bg-muted">
          <TableRow class="hover:bg-muted">
            <TableHead class="pl-4 text-xs w-40">Timestamp</TableHead>
            <TableHead class="text-xs w-20">Level</TableHead>
            <TableHead class="text-xs w-44">Logger</TableHead>
            <TableHead class="text-xs">Message</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          <TableRow v-for="line in lines" :key="line.id" class="align-top hover:bg-muted/30">
            <TableCell class="pl-4 text-xs font-mono text-muted-foreground whitespace-nowrap">
              {{ formatDate(line.ts) }}
            </TableCell>
            <TableCell :class="['text-xs font-mono', levelClass(line.level)]">
              {{ line.level }}
            </TableCell>
            <TableCell
              class="text-xs font-mono text-muted-foreground truncate"
              :title="line.logger"
            >
              {{ line.logger }}
            </TableCell>
            <TableCell class="text-xs">
              <pre class="font-mono break-all whitespace-pre-wrap">{{ line.message }}</pre>
            </TableCell>
          </TableRow>
        </TableBody>
      </Table>

      <!-- Mobile cards (< md) -->
      <div class="md:hidden space-y-2">
        <Card v-for="line in lines" :key="line.id">
          <CardContent class="px-3 py-2 space-y-1 text-xs">
            <div class="flex flex-wrap gap-x-3 font-mono text-muted-foreground">
              <span>{{ formatDate(line.ts) }}</span>
              <span :class="levelClass(line.level)">{{ line.level }}</span>
              <span class="truncate">{{ line.logger }}</span>
            </div>
            <pre class="font-mono break-all whitespace-pre-wrap leading-relaxed">{{
              line.message
            }}</pre>
          </CardContent>
        </Card>
      </div>
    </div>
  </div>
</template>
