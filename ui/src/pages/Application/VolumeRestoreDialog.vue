<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { toast } from 'vue-sonner'
import { ArchiveRestore, Download } from 'lucide-vue-next'

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
import {
  type Application,
  type Backup,
  downloadVolumeBackup,
  fetchVolumeBackups,
  refusesRequests,
  restoreVolumeBackup,
  type Volume,
} from '@/services/api'
import { formatDate } from '@/lib/utils'

// The volume's backups, each downloadable, and restoring one into a stopped container of the application, into this
// volume or another of the application's; the page is loaded again once the restore is queued.
const props = defineProps<{
  application: Application
  volume: Volume
  loadApplication: () => Promise<void>
}>()

const isOpen = ref(false)
const isLoading = ref(false)
const isRestoring = ref(false)
const loadError = ref('')
const backups = ref<Backup[]>([])
const selectedBackupId = ref('')
const selectedContainerId = ref('')
const selectedVolume = ref('')

const selectedBackup = computed(
  () => backups.value.find(backup => backup.id === selectedBackupId.value) ?? null
)
const selectedContainer = computed(
  () =>
    props.application.containers.find(container => container.id === selectedContainerId.value) ??
    null
)
const containerStopped = computed(
  () =>
    !!selectedContainer.value &&
    selectedContainer.value.view.headline === 'inactive' &&
    !refusesRequests(selectedContainer.value)
)
const restoreDisabled = computed(
  () =>
    !selectedBackup.value || !containerStopped.value || !selectedVolume.value || isRestoring.value
)

async function loadBackups() {
  isLoading.value = true
  loadError.value = ''

  try {
    backups.value = await fetchVolumeBackups(
      props.application.project.name,
      props.application.name,
      props.volume.name
    )
    selectedBackupId.value = backups.value[0]?.id ?? ''
  } catch (err) {
    loadError.value = err instanceof Error ? err.message : 'Failed to load backups'
  } finally {
    isLoading.value = false
  }
}

async function onClickDownload() {
  if (!selectedBackup.value) return

  try {
    await downloadVolumeBackup(
      props.application.project.name,
      props.application.name,
      props.volume.name,
      selectedBackup.value.id
    )
  } catch (err) {
    toast.error(err instanceof Error ? err.message : 'Failed to download backup')
  }
}

async function onClickRestore() {
  if (!selectedBackup.value || !selectedContainer.value) return

  isRestoring.value = true
  try {
    await restoreVolumeBackup(
      props.application.project.name,
      props.application.name,
      props.volume.name,
      selectedBackup.value.id,
      selectedContainer.value.id,
      selectedVolume.value
    )
    toast.success(`Restore queued for volume ${selectedVolume.value}`)
    isOpen.value = false
    await props.loadApplication()
  } catch (err) {
    toast.error(err instanceof Error ? err.message : 'Failed to restore volume')
  } finally {
    isRestoring.value = false
  }
}

watch(isOpen, async open => {
  if (!open) return

  selectedContainerId.value = props.application.containers[0]?.id ?? ''
  selectedVolume.value = props.volume.name
  await loadBackups()
})
</script>

<template>
  <Dialog v-model:open="isOpen">
    <DialogTrigger asChild>
      <Button size="sm" class="flex-1" variant="outline"> <ArchiveRestore />Restore </Button>
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
        Restore is destructive. The target volume of the chosen container is cleared before the
        backup is extracted into it.
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
                <SelectItem v-for="backup in backups" :key="backup.id" :value="backup.id">
                  {{ formatDate(backup.created_at) }}
                </SelectItem>
              </SelectContent>
            </Select>

            <div
              v-if="selectedBackup"
              class="rounded-md border p-3 text-sm text-muted-foreground space-y-1"
            >
              <p>Taken: {{ formatDate(selectedBackup.created_at) }}</p>
              <p>Of volume: {{ selectedBackup.source_volume_name }}</p>
              <p class="break-all">S3 Path: {{ selectedBackup.s3_path }}</p>
            </div>
          </div>
        </div>

        <div class="space-y-2">
          <Label>Into the container on</Label>
          <Select v-model="selectedContainerId">
            <SelectTrigger>
              <SelectValue placeholder="Select a container" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem
                v-for="container in props.application.containers"
                :key="container.id"
                :value="container.id"
              >
                {{ container.worker.hostname }} ({{ container.view.headline }})
              </SelectItem>
            </SelectContent>
          </Select>
        </div>

        <div class="space-y-2">
          <Label>Into the volume</Label>
          <Select v-model="selectedVolume">
            <SelectTrigger>
              <SelectValue placeholder="Select a volume" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem
                v-for="volume in props.application.volumes"
                :key="volume.id"
                :value="volume.name"
              >
                {{ volume.name }}
              </SelectItem>
            </SelectContent>
          </Select>
        </div>

        <p v-if="selectedContainer && !containerStopped" class="text-sm text-muted-foreground">
          A restore needs the container stopped. You can still browse and download the backups.
        </p>
      </div>

      <DialogFooter>
        <Button size="sm" type="button" variant="outline" @click="isOpen = false">Close</Button>
        <Button
          size="sm"
          type="button"
          variant="outline"
          :disabled="!selectedBackup"
          @click="onClickDownload"
        >
          <Download />Download
        </Button>
        <Button size="sm" :disabled="restoreDisabled" @click="onClickRestore">
          <Spinner class="animate-spin" v-if="isRestoring" />
          <ArchiveRestore v-else />Restore
        </Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>
