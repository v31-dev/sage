<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import { Card, CardContent } from '@/components/ui/card'
import { Spinner } from '@/components/ui/spinner'
import { TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { useFeedList } from '@/lib/feedList'
import { formatDate, levelClass } from '@/lib/utils'
import { fetchTaskLogs, type TaskLogLine, type TaskPart } from '@/services/api'

// Every line of a task, or of one container's part of it, oldest first, followed as they are written. All of a task's
// lines show which of its `parts` each is from.
const props = withDefaults(
  defineProps<{ taskId: string; subTaskId?: string; parts?: TaskPart[] }>(),
  { subTaskId: '', parts: () => [] }
)

const viewport = ref<HTMLElement | null>(null)

const showParts = computed(() => !props.subTaskId && props.parts.length > 0)
const partsById = computed(() => new Map(props.parts.map(part => [part.id, part])))

const partLabel = (id: string) => (id ? (partsById.value.get(id)?.worker ?? id) : '')

const {
  rows: lines,
  loading,
  error,
} = useFeedList<TaskLogLine>({
  topic: `task-logs:${props.subTaskId ? `${props.taskId}/${props.subTaskId}` : props.taskId}`,
  order: (a, b) => a.id - b.id,
  newestFirst: false,
  key: line => line.id,
  load: async (_query, newest) => ({
    rows: (await fetchTaskLogs(props.taskId, props.subTaskId, newest?.id ?? 0)).lines,
  }),
})

function atBottom() {
  const view = viewport.value
  return !view || view.scrollHeight - (view.scrollTop + view.clientHeight) <= 50
}

// The view follows new lines while it is at the bottom (measured before they are shown).
watch(
  () => lines.value[lines.value.length - 1]?.id,
  async () => {
    if (!atBottom() || !viewport.value) return
    await nextTick()
    viewport.value.scrollTop = viewport.value.scrollHeight
  }
)
</script>

<template>
  <div class="flex flex-col min-h-0 gap-2">
    <p v-if="error" class="text-sm text-destructive">{{ error }}</p>
    <div v-if="loading" class="flex justify-center py-12">
      <Spinner class="animate-spin" />
    </div>
    <Card v-else-if="!lines.length">
      <CardContent class="flex items-center justify-center py-12">
        <p class="text-muted-foreground">No logs found</p>
      </CardContent>
    </Card>
    <div v-else ref="viewport" class="min-h-0 overflow-y-auto md:border md:rounded-lg md:bg-card">
      <!-- Desktop table (md+) -->
      <table class="hidden md:table w-full caption-bottom text-sm table-fixed">
        <TableHeader class="sticky top-0 z-10 bg-muted">
          <TableRow class="hover:bg-muted">
            <TableHead class="pl-4 text-xs w-40">Timestamp</TableHead>
            <TableHead class="text-xs w-20">Level</TableHead>
            <TableHead class="text-xs w-44">Logger</TableHead>
            <TableHead v-if="showParts" class="text-xs w-36">Part</TableHead>
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
            <TableCell
              v-if="showParts"
              class="text-xs font-mono text-muted-foreground truncate"
              :title="line.sub_task_id"
            >
              {{ partLabel(line.sub_task_id) }}
            </TableCell>
            <TableCell class="text-xs">
              <pre class="font-mono break-all whitespace-pre-wrap">{{ line.message }}</pre>
            </TableCell>
          </TableRow>
        </TableBody>
      </table>

      <!-- Mobile cards (< md) -->
      <div class="md:hidden space-y-2">
        <Card v-for="line in lines" :key="line.id">
          <CardContent class="px-3 py-2 space-y-1 text-xs">
            <div class="flex flex-wrap gap-x-3 font-mono text-muted-foreground">
              <span>{{ formatDate(line.ts) }}</span>
              <span :class="levelClass(line.level)">{{ line.level }}</span>
              <span class="truncate">{{ line.logger }}</span>
              <span v-if="showParts && line.sub_task_id">{{ partLabel(line.sub_task_id) }}</span>
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
