<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { toast } from 'vue-sonner'
import { ArchiveRestore } from 'lucide-vue-next'

import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from '@/components/ui/dialog'
import { Button } from '@/components/ui/button'
import { Label } from '@/components/ui/label'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { Spinner } from '@/components/ui/spinner'
import { type Application, type Backup, getVolumeBackupAPI, type Volume } from '@/services/api'
import { useAppStore } from '@/stores/app'
import RestoreLogsDialog from '@/components/RestoreLogsDialog.vue'
import { formatDate } from '@/lib/utils'

interface Props {
  application: Application
  volume: Volume
  loadApplication: () => Promise<void>
}

const props = withDefaults(defineProps<Props>(), {})

const appStore = useAppStore()
const volumeBackupAPI = getVolumeBackupAPI(
  props.application.project.name,
  props.application.name,
  props.volume.name
)

const isOpen = ref(false)
const isLoading = ref(false)
const isRestoring = ref(false)
const loadError = ref('')
const backups = ref<Backup[]>([])
const selectedBackupId = ref('')
const selectedTargetWorker = ref('')
const restoreDialogTaskId = ref('')

const canStartRestore = computed(() => {
  return [
    !selectedBackup.value,
    !selectedTargetWorker.value,
    isRestoring.value,
    appStore.applicationDeployStatus !== 'inactive',
    props.application.status !== 'inactive',
    targetWorkers.value.length === 0,
  ].includes(true)
})

const targetWorkers = computed(() => {
  return props.application.containers.map(container => container.worker.hostname)
})

const selectedBackup = computed(() => {
  return backups.value.find(backup => String(backup.id) === selectedBackupId.value) || null
})

function parseBackupPath(s3Path: string) {
  const parts = s3Path.split('/').filter(Boolean)
  const filename = parts[parts.length - 1] || ''
  const rawTimestamp = filename.replace(/\.tar\.gz\.enc$/, '') || '-'

  return {
    worker: parts[parts.length - 3] || '-',
    timestamp: formatBackupTimestamp(rawTimestamp),
  }
}

function formatBackupTimestamp(rawTimestamp: string) {
  const match = rawTimestamp.match(/^(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})$/)
  if (!match) return rawTimestamp

  const [, year, month, day, hour, minute, second] = match
  return formatDate(`${year}-${month}-${day}T${hour}:${minute}:${second}`)
}

async function loadBackups() {
  isLoading.value = true
  loadError.value = ''

  try {
    const response = (await volumeBackupAPI.fetchAll()) as Backup[]
    backups.value = [...response].reverse()
    selectedBackupId.value = backups.value[0] ? String(backups.value[0].id) : ''
  } catch (err) {
    loadError.value = err instanceof Error ? err.message : 'Failed to load backups'
  } finally {
    isLoading.value = false
  }
}

async function onClickRestore() {
  if (!selectedBackup.value || !selectedTargetWorker.value) return

  const previousStatus = appStore.applicationDeployStatus
  isRestoring.value = true
  appStore.updateApplicationDeployStatus('restoring')

  try {
    await volumeBackupAPI.action(`${selectedBackup.value.id}/restore`, {
      target_worker: selectedTargetWorker.value,
    })
    toast.success(`Restore completed for volume ${props.volume.name}`)
    isOpen.value = false
  } catch (err) {
    const response = (err as any)?.response
    const taskId = response?.status >= 500 ? response?.headers?.get('X-Task-ID') || '' : ''
    if (taskId) {
      restoreDialogTaskId.value = taskId
    }
    toast.error(err instanceof Error ? err.message : 'Failed to restore volume')
  } finally {
    isRestoring.value = false
    appStore.updateApplicationDeployStatus(previousStatus)
    await props.loadApplication()
  }
}

watch(isOpen, async open => {
  if (!open) return

  selectedTargetWorker.value = targetWorkers.value[0] || ''
  await loadBackups()
})
</script>

<template>
  <Dialog v-model:open="isOpen">
    <DialogTrigger asChild>
      <Button size="sm" class="flex-1" variant="outline">
        <ArchiveRestore />Restore
      </Button>
    </DialogTrigger>
    <DialogContent class="sm:max-w-xl">
      <DialogHeader>
        <DialogTitle>Restore Volume</DialogTitle>
        <DialogDescription>
          Backups for volume {{ props.volume.name }}. Restore will wipe the selected target volume
          before extracting backup contents.
        </DialogDescription>
      </DialogHeader>

      <div class="rounded-md border bg-destructive/5 text-sm text-muted-foreground p-3">
        Restore is destructive. The selected target worker volume will be cleared before restore.
        Multi-container targeting is user-controlled.
      </div>

      <div class="space-y-4">
        <div class="space-y-2">
          <Label>Backup</Label>
          <div v-if="isLoading" class="flex items-center justify-center py-8 rounded-md border">
            <Spinner class="animate-spin" />
          </div>
          <div v-else-if="loadError" class="text-sm text-destructive">
            {{ loadError }}
          </div>
          <div v-else-if="backups.length === 0" class="text-sm text-muted-foreground py-4">
            No backups found for this volume.
          </div>
          <div v-else class="space-y-3">
            <Select v-model="selectedBackupId">
              <SelectTrigger>
                <SelectValue placeholder="Select backup" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem v-for="backup in backups" :key="backup.id" :value="String(backup.id)">
                  {{ parseBackupPath(backup.s3_path).timestamp }} on
                  {{ parseBackupPath(backup.s3_path).worker }}
                </SelectItem>
              </SelectContent>
            </Select>

            <div v-if="selectedBackup" class="rounded-md border p-3 text-sm text-muted-foreground space-y-1">
              <p>Source Worker: {{ parseBackupPath(selectedBackup.s3_path).worker }}</p>
              <p>Backup Timestamp: {{ parseBackupPath(selectedBackup.s3_path).timestamp }}</p>
              <p class="break-all">S3 Path: {{ selectedBackup.s3_path }}</p>
            </div>
          </div>
        </div>

        <div class="space-y-2">
          <Label>Target Worker</Label>
          <Select v-model="selectedTargetWorker">
            <SelectTrigger>
              <SelectValue placeholder="Select target worker" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem v-for="worker in targetWorkers" :key="worker" :value="worker">
                {{ worker }}
              </SelectItem>
            </SelectContent>
          </Select>
        </div>

        <p v-if="props.application.status !== 'inactive'" class="text-sm text-muted-foreground">
          Restore can only be started while the application is inactive. You can still browse
          available backups now.
        </p>
      </div>

      <DialogFooter>
        <Button size="sm" type="button" variant="outline" @click="isOpen = false">Close</Button>
        <Button size="sm" :disabled="canStartRestore" @click="onClickRestore">
          <Spinner class="animate-spin" v-if="isRestoring" />
          <ArchiveRestore v-else />Restore
        </Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>

  <RestoreLogsDialog
    :taskId="restoreDialogTaskId"
    :objectTitle="`${props.volume.name} on ${selectedTargetWorker || 'selected worker'}`"
  />
</template>
