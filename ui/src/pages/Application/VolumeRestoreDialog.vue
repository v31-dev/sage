<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue'
import { toast } from 'vue-sonner'
import { ArchiveRestore, Download, Trash } from 'lucide-vue-next'

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
import { Collapsible, CollapsibleContent } from '@/components/ui/collapsible'
import {
  Item,
  ItemActions,
  ItemContent,
  ItemDescription,
  ItemFooter,
  ItemGroup,
  ItemTitle,
} from '@/components/ui/item'
import { Label } from '@/components/ui/label'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { Spinner } from '@/components/ui/spinner'
import BackupState from '@/components/BackupState.vue'
import ConfirmationButton from '@/components/ConfirmationButton.vue'
import {
  type Application,
  type Backup,
  deleteVolumeBackup,
  downloadVolumeBackup,
  fetchVolumeBackups,
  refusesRequests,
  restoreVolumeBackup,
  type Volume,
} from '@/services/api'
import { watchState } from '@/lib/updates'
import { formatDate } from '@/lib/utils'

// The volume's backups, newest first, as the manager pushes them while the dialog is open: each taken one can be
// downloaded, deleted or restored into a stopped container of the application, into this volume or another of the
// application's, and another finished one deleted. One row's restore is open at a time; the page is loaded again
// once a restore is queued.
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
const restoringId = ref('')
const selectedContainerId = ref('')
const selectedVolume = ref('')
let stop: (() => void) | undefined

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
  () => !containerStopped.value || !selectedVolume.value || isRestoring.value
)

const finished = (backup: Backup) => backup.status !== 'queued' && backup.status !== 'running'

const details = (backup: Backup) =>
  [
    `Volume ${backup.source_volume_name}`,
    backup.automatic ? 'scheduled' : 'on request',
    backup.worker_hostname && `from ${backup.worker_hostname}`,
  ]
    .filter(Boolean)
    .join(' · ')

function showBackups(next: Backup[]) {
  backups.value = next
  if (!next.some(backup => backup.id === restoringId.value && backup.status === 'succeeded')) {
    restoringId.value = ''
  }
}

function openRestore(backup: Backup) {
  restoringId.value = restoringId.value === backup.id ? '' : backup.id
  selectedContainerId.value = props.application.containers[0]?.id ?? ''
  selectedVolume.value = props.volume.name
}

async function onClickDownload(backup: Backup) {
  try {
    await downloadVolumeBackup(
      props.application.project.name,
      props.application.name,
      props.volume.name,
      backup.id
    )
  } catch (err) {
    toast.error(err instanceof Error ? err.message : 'Failed to download backup')
  }
}

async function onConfirmDelete(backup: Backup) {
  try {
    const { task_id } = await deleteVolumeBackup(
      props.application.project.name,
      props.application.name,
      props.volume.name,
      backup.id
    )
    toast.success(task_id ? 'Backup deletion queued' : 'Backup deleted')
  } catch (err) {
    toast.error(err instanceof Error ? err.message : 'Failed to delete backup')
  }
}

async function onClickRestore(backup: Backup) {
  if (!selectedContainer.value) return

  isRestoring.value = true
  try {
    await restoreVolumeBackup(
      props.application.project.name,
      props.application.name,
      props.volume.name,
      backup.id,
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
  stop?.()
  stop = undefined
  if (!open) return

  restoringId.value = ''
  isLoading.value = true
  loadError.value = ''
  const followed = watchState(
    `volume-backups:${props.volume.id}`,
    () =>
      fetchVolumeBackups(props.application.project.name, props.application.name, props.volume.name),
    showBackups
  )
  stop = followed.stop
  try {
    await followed.first
  } catch (err) {
    loadError.value = err instanceof Error ? err.message : 'Failed to load backups'
  } finally {
    isLoading.value = false
  }
})
onUnmounted(() => stop?.())
</script>

<template>
  <Dialog v-model:open="isOpen">
    <DialogTrigger asChild>
      <Button size="sm" class="flex-1" variant="outline"> <ArchiveRestore />Restore </Button>
    </DialogTrigger>
    <DialogContent class="flex flex-col sm:max-w-3xl">
      <DialogHeader>
        <DialogTitle>Backups of {{ props.volume.name }}</DialogTitle>
        <DialogDescription>
          Each backup of this volume, newest first. A restore clears the chosen volume of the chosen
          container before the backup is put in its place.
        </DialogDescription>
      </DialogHeader>

      <div v-if="isLoading" class="flex items-center justify-center py-8 rounded-md border">
        <Spinner class="animate-spin" />
      </div>
      <div v-else-if="loadError" class="text-sm text-destructive">
        {{ loadError }}
      </div>
      <div v-else-if="backups.length === 0" class="text-sm text-muted-foreground py-4">
        No backups of this volume yet.
      </div>
      <ItemGroup v-else class="min-h-0 flex-1 overflow-y-auto gap-2">
        <Collapsible
          v-for="backup in backups"
          :key="backup.id"
          :open="restoringId === backup.id"
          asChild
        >
          <Item variant="outline" class="flex-wrap" :data-backup="backup.id">
            <ItemContent class="min-w-60">
              <ItemTitle>{{ formatDate(backup.created_at) }}</ItemTitle>
              <ItemDescription>{{ details(backup) }}</ItemDescription>
              <BackupState :backup="backup" />
            </ItemContent>
            <ItemActions v-if="finished(backup)" class="gap-2">
              <template v-if="backup.status === 'succeeded'">
                <Button size="sm" variant="outline" @click="onClickDownload(backup)">
                  <Download />Download
                </Button>
                <Button
                  size="sm"
                  variant="outline"
                  :aria-expanded="restoringId === backup.id"
                  @click="openRestore(backup)"
                >
                  <ArchiveRestore />Restore
                </Button>
              </template>
              <ConfirmationButton
                triggerText="Delete"
                :title="'Delete backup ' + formatDate(backup.created_at)"
                body="This backup is deleted from the store. This can't be undone."
                :description="`Backup of ${backup.source_volume_name}`"
                :icon="Trash"
                destructive
                :onConfirm="() => onConfirmDelete(backup)"
              />
            </ItemActions>
            <CollapsibleContent asChild>
              <ItemFooter class="basis-full flex-col items-stretch gap-3" data-restore>
                <div class="grid gap-3 sm:grid-cols-2">
                  <div class="space-y-2">
                    <Label>Into the container on</Label>
                    <Select v-model="selectedContainerId">
                      <SelectTrigger class="w-full">
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
                      <SelectTrigger class="w-full">
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
                </div>
                <p
                  v-if="selectedContainer && !containerStopped"
                  class="text-sm text-muted-foreground"
                >
                  A restore needs the container stopped.
                </p>
                <div class="flex justify-end gap-2">
                  <Button size="sm" variant="outline" @click="restoringId = ''">Cancel</Button>
                  <Button size="sm" :disabled="restoreDisabled" @click="onClickRestore(backup)">
                    <Spinner class="animate-spin" v-if="isRestoring" />
                    <ArchiveRestore v-else />Restore here
                  </Button>
                </div>
              </ItemFooter>
            </CollapsibleContent>
          </Item>
        </Collapsible>
      </ItemGroup>

      <DialogFooter>
        <Button size="sm" type="button" variant="outline" @click="isOpen = false">Close</Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>
