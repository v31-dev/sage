<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue'
import {
  AlertDialog,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog'
import { Spinner } from '@/components/ui/spinner'
import { formatDate } from '@/lib/utils'
import { watchState } from '@/lib/updates'
import { fetchTasks, type QueueResponse } from '@/services/api'
import { useAppStore } from '@/stores/app'

const appStore = useAppStore()
const restoring = computed(() => appStore.info?.state === 'restoring')
const restoringFrom = computed(() => appStore.info?.restoring_from)

// The restore itself is among the running tasks; it is not what the restore waits for.
const waitingFor = ref(0)

function showWaitingFor(tasks: QueueResponse) {
  waitingFor.value = tasks.running.filter(task => task.name !== 'restore_database_from_s3').length
}

// The state is held by the manager, so a refreshed page shows the same overlay. The running
// tasks are followed only while restoring.
let stopTasks: (() => void) | undefined
watch(
  restoring,
  (now, before) => {
    stopTasks?.()
    stopTasks = undefined
    if (now) {
      const tasks = watchState('tasks', fetchTasks, showWaitingFor)
      stopTasks = tasks.stop
      tasks.first.catch(err => console.error('Failed to load the running tasks:', err))
    }
    // When the restore ends the data under every page has changed: reload them all.
    if (before && !now) location.reload()
  },
  { immediate: true }
)

onUnmounted(() => stopTasks?.())
</script>

<template>
  <AlertDialog :open="restoring">
    <AlertDialogContent>
      <AlertDialogHeader>
        <AlertDialogTitle class="flex items-center gap-2">
          <Spinner class="animate-spin" />
          Restoring Sage
        </AlertDialogTitle>
        <AlertDialogDescription>
          <template v-if="restoringFrom">
            Restoring the backup of {{ formatDate(restoringFrom) }}.
          </template>
          <template v-else>Restoring a backup.</template>
          Sage is blocked until it finishes, and this page reloads when it is done.
          <template v-if="waitingFor > 0">
            Waiting for {{ waitingFor }} running {{ waitingFor === 1 ? 'task' : 'tasks' }} to finish
            first.
          </template>
        </AlertDialogDescription>
      </AlertDialogHeader>
    </AlertDialogContent>
  </AlertDialog>
</template>
