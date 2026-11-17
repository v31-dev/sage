<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { History, TriangleAlert } from 'lucide-vue-next'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from '@/components/ui/dialog'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { Field, FieldLabel } from '@/components/ui/field'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { Spinner } from '@/components/ui/spinner'
import { TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { formatDate, levelClass } from '@/lib/utils'
import { fetchMigrations, type MigrationRun } from '@/services/api'

// `run` is the run to show first, the newest when it is not listed; with `open` the dialog opens on it at once.
const props = defineProps<{ run?: string; open?: boolean }>()

const TRIGGERS: Record<MigrationRun['trigger'], string> = {
  boot: 'At start',
  restore: 'Platform restore',
}

const open = ref(false)
const loading = ref(false)
const error = ref('')
const runs = ref<MigrationRun[]>([])
const selected = ref('')
// Only the newest load's answer is shown.
let loads = 0

const run = computed(() => runs.value.find(candidate => candidate.run === selected.value))

function label(candidate: MigrationRun) {
  return `${formatDate(candidate.at)} · Sage ${candidate.release} · ${TRIGGERS[candidate.trigger]}${candidate.refused ? ' · Refused' : ''}`
}

async function load() {
  const load = ++loads
  loading.value = true
  error.value = ''
  try {
    const answer = await fetchMigrations()
    if (load !== loads) return
    runs.value = answer
    selected.value =
      answer.find(candidate => candidate.run === props.run)?.run ?? answer[0]?.run ?? ''
  } catch (err) {
    if (load !== loads) return
    error.value = err instanceof Error ? err.message : 'Failed to load the migration logs'
  } finally {
    if (load === loads) loading.value = false
  }
}

watch(
  () => props.open,
  shown => {
    if (shown) {
      open.value = true
      load()
    }
  },
  { immediate: true }
)
</script>

<template>
  <Dialog v-model:open="open">
    <DialogTrigger asChild>
      <Button variant="outline" size="sm" @click="load"><History />Migration logs</Button>
    </DialogTrigger>
    <DialogContent class="flex flex-col sm:max-w-6xl">
      <DialogHeader>
        <DialogTitle>Migration logs</DialogTitle>
        <DialogDescription>
          Each time Sage brought its data up to a new version, and each time it couldn't, with what
          is left to do by hand at the end.
        </DialogDescription>
      </DialogHeader>
      <div v-if="loading" class="flex justify-center py-6"><Spinner /></div>
      <p v-else-if="error" class="text-sm text-destructive" data-error>{{ error }}</p>
      <p v-else-if="runs.length === 0" class="text-sm text-muted-foreground" data-empty>
        No migrations yet.
      </p>
      <template v-else>
        <Field>
          <FieldLabel for="migration-run">Run</FieldLabel>
          <Select v-model="selected">
            <SelectTrigger id="migration-run">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem v-for="candidate in runs" :key="candidate.run" :value="candidate.run">
                {{ label(candidate) }}
              </SelectItem>
            </SelectContent>
          </Select>
        </Field>
        <Alert v-if="run?.actions" variant="destructive" data-actions-note>
          <TriangleAlert />
          <AlertDescription>
            This run left {{ run.actions }} {{ run.actions === 1 ? 'step' : 'steps' }} to do
            manually, in the exact order shown. They're the lines marked CRITICAL at the end of the
            log.
          </AlertDescription>
        </Alert>
        <div v-if="run" class="min-h-0 flex-1 overflow-y-auto md:border md:rounded-lg md:bg-card">
          <!-- Desktop table (md+) -->
          <table class="hidden md:table w-full caption-bottom text-sm table-fixed">
            <TableHeader class="sticky top-0 z-10 bg-muted">
              <TableRow class="hover:bg-muted">
                <TableHead class="pl-4 text-xs w-40">Timestamp</TableHead>
                <TableHead class="text-xs w-24">Level</TableHead>
                <TableHead class="text-xs">Message</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              <TableRow
                v-for="(line, index) in run.lines"
                :key="index"
                class="align-top hover:bg-muted/30"
                data-line
              >
                <TableCell class="pl-4 text-xs font-mono text-muted-foreground whitespace-nowrap">
                  {{ formatDate(line.ts) }}
                </TableCell>
                <TableCell :class="['text-xs font-mono', levelClass(line.level)]">
                  {{ line.level }}
                </TableCell>
                <TableCell class="text-xs">
                  <pre class="font-mono break-all whitespace-pre-wrap">{{ line.message }}</pre>
                </TableCell>
              </TableRow>
            </TableBody>
          </table>

          <!-- Mobile cards (< md) -->
          <div class="md:hidden space-y-2">
            <Card v-for="(line, index) in run.lines" :key="index">
              <CardContent class="px-3 py-2 space-y-1 text-xs">
                <div class="flex flex-wrap gap-x-3 font-mono text-muted-foreground">
                  <span>{{ formatDate(line.ts) }}</span>
                  <span :class="levelClass(line.level)">{{ line.level }}</span>
                </div>
                <pre class="font-mono break-all whitespace-pre-wrap leading-relaxed">{{
                  line.message
                }}</pre>
              </CardContent>
            </Card>
          </div>
        </div>
      </template>
      <DialogFooter>
        <Button size="sm" type="button" variant="outline" @click="open = false">Close</Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>
