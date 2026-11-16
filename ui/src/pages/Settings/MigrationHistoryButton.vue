<script setup lang="ts">
import { computed, ref } from 'vue'
import { History } from 'lucide-vue-next'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from '@/components/ui/dialog'
import { Field, FieldLabel } from '@/components/ui/field'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { Spinner } from '@/components/ui/spinner'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import CodeBlock from '@/components/CodeBlock.vue'
import StateIndicator from '@/components/StateIndicator.vue'
import { formatDate } from '@/lib/utils'
import {
  fetchMigrations,
  type IndicatorState,
  type MigrationFile,
  type MigrationRun,
} from '@/services/api'

const OUTCOMES: Record<MigrationFile['outcome'], { state: IndicatorState; label: string }> = {
  migrated: { state: 'ok', label: 'Updated' },
  recreated: { state: 'warning', label: 'Started again empty' },
  refused: { state: 'error', label: 'Refused' },
}
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
const actions = computed(() => run.value?.files.flatMap(file => file.actions) ?? [])

function label(candidate: MigrationRun) {
  return `${formatDate(candidate.at)} · Sage ${candidate.release} · ${TRIGGERS[candidate.trigger]} · ${OUTCOMES[candidate.outcome].label}`
}

async function load() {
  const load = ++loads
  loading.value = true
  error.value = ''
  try {
    const answer = await fetchMigrations()
    if (load !== loads) return
    runs.value = answer
    selected.value = answer[0]?.run ?? ''
  } catch (err) {
    if (load !== loads) return
    error.value = err instanceof Error ? err.message : 'Failed to load the migration history'
  } finally {
    if (load === loads) loading.value = false
  }
}
</script>

<template>
  <Dialog v-model:open="open">
    <DialogTrigger asChild>
      <Button variant="outline" size="sm" @click="load"><History />Migration history</Button>
    </DialogTrigger>
    <DialogContent class="sm:max-w-4xl">
      <DialogHeader>
        <DialogTitle>Migration history</DialogTitle>
        <DialogDescription>
          Each time Sage brought its data up to a new version, and each time it couldn't.
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
        <div v-if="run" class="max-h-[60vh] overflow-auto">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>File</TableHead>
                <TableHead>Version</TableHead>
                <TableHead>Outcome</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              <TableRow v-for="file in run.files" :key="file.file + file.at" data-file>
                <TableCell class="font-mono text-xs">{{ file.file }}</TableCell>
                <TableCell>{{ file.from === null ? '—' : `${file.from} → ${file.to}` }}</TableCell>
                <TableCell class="whitespace-normal">
                  <StateIndicator
                    :state="OUTCOMES[file.outcome].state"
                    :label="OUTCOMES[file.outcome].label"
                    showLabel
                  />
                  <p v-if="file.detail" class="mt-1 text-xs text-muted-foreground">
                    {{ file.detail }}
                  </p>
                </TableCell>
              </TableRow>
            </TableBody>
          </Table>
          <div v-if="actions.length" class="mt-4 space-y-3" data-actions>
            <p class="text-sm font-medium">What to do by hand, in this order</p>
            <ol class="list-decimal space-y-3 pl-5 text-sm">
              <li v-for="(action, index) in actions" :key="index" data-action>
                <p>
                  <span class="font-medium">{{ action.where }}:</span>
                  {{ action.why }}
                </p>
                <CodeBlock v-if="action.command" :code="action.command" class="mt-1" />
              </li>
            </ol>
          </div>
        </div>
      </template>
      <DialogFooter>
        <Button size="sm" type="button" variant="outline" @click="open = false">Close</Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>
