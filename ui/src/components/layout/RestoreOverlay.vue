<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import {
  AlertDialog,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog'
import { Spinner } from '@/components/ui/spinner'
import { formatDate } from '@/lib/utils'
import { tasksAPI, type TasksResponse } from '@/services/api'
import { useAppStore } from '@/stores/app'

const appStore = useAppStore()
const restoring = computed(() => appStore.info?.state === 'restoring')
const restoringFrom = computed(() => appStore.info?.restoring_from)

// The restore itself is among the running tasks; it is not what the restore waits for.
const waitingFor = ref(0)

async function loadWaitingFor() {
  try {
    const tasks = (await tasksAPI.fetchAll()) as unknown as TasksResponse
    waitingFor.value = tasks.running.filter(task => task.name !== 'restore_database_from_s3').length
  } catch (err) {
    console.error('Failed to load the running tasks:', err)
  }
}

// The state is held by the manager, so a refreshed page shows the same overlay. The app
// store polls every 2 s while restoring, and each of its updates refreshes the count.
watch(
  () => appStore.info,
  () => {
    if (restoring.value) loadWaitingFor()
  },
  { immediate: true }
)

// When the restore ends the data under every page has changed: reload them all.
watch(restoring, (now, before) => {
  if (before && !now) location.reload()
})
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
