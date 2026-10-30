<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { Download, Trash, TriangleAlert } from 'lucide-vue-next'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Skeleton } from '@/components/ui/skeleton'
import { Spinner } from '@/components/ui/spinner'
import {
  Table,
  TableBody,
  TableCell,
  TableEmpty,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import ConfirmationButton from '@/components/ConfirmationButton.vue'
import StateIndicator from '@/components/StateIndicator.vue'
import { formatDate, parseDate } from '@/lib/utils'
import { deleteDebugFile, downloadDebugFile, setDebugStop } from '@/services/api'
import { useAppStore } from '@/stores/app'

// The capture as the app store follows it.
const appStore = useAppStore()
const stopAt = computed(() => appStore.debugCapture?.stop_at ?? null)
const files = computed(() => appStore.debugCapture?.files ?? [])
const loading = computed(() => appStore.debugCapture === null && !appStore.debugCaptureError)
const saving = ref(false)
const saveError = ref('')

// A datetime-local input's value: the local date and time, to the minute.
function inputValue(date: Date) {
  return new Date(date.getTime() - date.getTimezoneOffset() * 60_000).toISOString().slice(0, 16)
}

const stopInput = ref(inputValue(new Date(Date.now() + 60 * 60_000)))
const on = computed(() => stopAt.value !== null)

function formatSize(bytes: number) {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KiB`
  return `${(bytes / 1024 ** 2).toFixed(1)} MiB`
}

// The field shows a new stop time, and otherwise keeps what is typed in it.
watch(
  stopAt,
  next => {
    if (next) stopInput.value = inputValue(parseDate(next))
  },
  { immediate: true }
)

async function save(next: string | null) {
  saving.value = true
  saveError.value = ''
  try {
    await setDebugStop(next)
    await appStore.refreshDebugCapture()
  } catch (err) {
    saveError.value = err instanceof Error ? err.message : 'Failed to change the debug capture'
  } finally {
    saving.value = false
  }
}

async function onDelete(name: string) {
  await deleteDebugFile(name)
  await appStore.refreshDebugCapture()
}
</script>

<template>
  <main class="flex-1 px-2 sm:px-4 py-4 sm:py-4 relative">
    <div class="max-w-7xl mx-auto flex flex-col gap-6">
      <Card>
        <CardHeader>
          <CardTitle>Debug capture</CardTitle>
          <CardDescription>
            Writes every line Sage logs to a file until the stop time, for you to download. A
            capture carries on after a restart, and stops early when its file reaches the size
            limit.
          </CardDescription>
        </CardHeader>
        <CardContent class="space-y-4 text-sm">
          <Alert v-if="appStore.debugCaptureError" variant="destructive">
            <TriangleAlert />
            <AlertDescription>{{ appStore.debugCaptureError }}</AlertDescription>
          </Alert>
          <Skeleton v-if="loading" class="h-9 w-80" />
          <template v-else>
            <StateIndicator
              :state="on ? 'busy' : 'idle'"
              :label="on && stopAt ? `Capturing until ${formatDate(stopAt)}` : 'Off'"
              show-label
            />
            <div class="flex flex-wrap items-center gap-2">
              <Input
                v-model="stopInput"
                type="datetime-local"
                aria-label="Stop time"
                class="w-auto"
              />
              <Button
                size="sm"
                :disabled="saving || !stopInput"
                @click="save(new Date(stopInput).toISOString())"
              >
                <Spinner v-if="saving" class="animate-spin" />
                {{ on ? 'Change stop time' : 'Start' }}
              </Button>
              <Button v-if="on" variant="outline" size="sm" :disabled="saving" @click="save(null)">
                Stop
              </Button>
            </div>
            <Alert v-if="saveError" variant="destructive">
              <TriangleAlert />
              <AlertDescription>{{ saveError }}</AlertDescription>
            </Alert>
          </template>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Files</CardTitle>
          <CardDescription>
            One per capture, named by its start time, and deleted after the retention period.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <Table>
            <TableHeader class="bg-muted/50">
              <TableRow>
                <TableHead>File</TableHead>
                <TableHead>Size</TableHead>
                <TableHead>Last written</TableHead>
                <TableHead />
              </TableRow>
            </TableHeader>
            <TableBody>
              <TableEmpty v-if="!files.length" :colspan="4">No captures</TableEmpty>
              <TableRow v-for="file in files" :key="file.name">
                <TableCell class="font-mono text-xs">
                  {{ file.name }}
                  <span v-if="file.active" class="ml-1 font-sans text-muted-foreground">
                    (being written)
                  </span>
                </TableCell>
                <TableCell class="text-xs">{{ formatSize(file.size) }}</TableCell>
                <TableCell class="text-xs whitespace-nowrap">{{
                  formatDate(file.modified_at)
                }}</TableCell>
                <TableCell>
                  <div class="flex justify-end gap-2">
                    <Button variant="outline" size="sm" @click="downloadDebugFile(file.name)">
                      <Download />Download
                    </Button>
                    <ConfirmationButton
                      triggerText="Delete"
                      :title="`Delete ${file.name}`"
                      body="Are you sure you want to delete this file? This action cannot be undone."
                      :icon="Trash"
                      destructive
                      :disabled="file.active"
                      :onConfirm="() => onDelete(file.name)"
                    />
                  </div>
                </TableCell>
              </TableRow>
            </TableBody>
          </Table>
        </CardContent>
      </Card>
    </div>
  </main>
</template>
