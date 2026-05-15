<script setup lang="ts">
import { ref } from 'vue'
import { toast } from 'vue-sonner'
import { Edit } from 'lucide-vue-next'
import {
  Card,
  CardHeader,
  CardTitle,
  CardAction,
  CardContent,
  CardFooter,
} from '@/components/ui/card'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Field, FieldGroup, FieldLabel, FieldSet, FieldError } from '@/components/ui/field'
import { Spinner } from '@/components/ui/spinner'
import { ButtonGroup } from '@/components/ui/button-group'
import { Label } from '@/components/ui/label'

import { type Volume, getVolumeAPI, type Application } from '@/services/api'
import TitleStatus from '@/components/TitleStatus.vue'
import ConfirmationButton from '@/components/ConfirmationButton.vue'
import VolumeBackupButton from './VolumeBackupButton.vue'
import VolumeRestoreDialog from './VolumeRestoreDialog.vue'

interface Props {
  application: Application
  volume: Volume
  volumeAPI: ReturnType<typeof getVolumeAPI>
  loadApplication: () => Promise<void>
}

const props = withDefaults(defineProps<Props>(), {})

const isEditVolumeDialogOpen = ref(false)
const volume = ref({
  name: '',
  path: '',
  backup_cron: '',
})
const editVolumeErrorMessage = ref('')
const isClickedEditVolumeConfirm = ref(false)

function openEditVolumeDialog() {
  editVolumeErrorMessage.value = ''
  volume.value.name = props.volume.name
  volume.value.path = props.volume.path
  volume.value.backup_cron = props.volume.backup_cron || ''
  isEditVolumeDialogOpen.value = true
}

async function onClickEditVolumeConfirm() {
  editVolumeErrorMessage.value = ''

  if (!volume.value.name.trim()) {
    editVolumeErrorMessage.value = 'Please enter a volume name'
    return
  }

  if (!volume.value.path.trim()) {
    editVolumeErrorMessage.value = 'Please enter a volume path'
    return
  }

  try {
    isClickedEditVolumeConfirm.value = true
    await props.volumeAPI.update(`${props.volume.name}`, volume.value)
    isEditVolumeDialogOpen.value = false
    await props.loadApplication()
    toast.success('Volume updated successfully')
  } catch (err) {
    editVolumeErrorMessage.value = err instanceof Error ? err.message : 'Failed to update volume'
  } finally {
    isClickedEditVolumeConfirm.value = false
  }
}

async function onClickConfirmDelete() {
  await props.volumeAPI.delete(`${props.volume.name}`)
  await props.loadApplication()
  toast.success('Volume deleted successfully')
}
</script>

<template>
  <Card>
    <CardHeader class="flex items-center justify-between border-b">
      <CardTitle>
        <TitleStatus :title="props.volume.name" />
      </CardTitle>
      <CardAction>
        <ButtonGroup class="space-x-1">
          <Button size="sm" variant="outline" @click="openEditVolumeDialog" class="gap-2">
            <Edit />
            Edit
          </Button>
          <ConfirmationButton
            title="Volume"
            mode="delete"
            :description="props.volume.name"
            :onConfirm="onClickConfirmDelete"
          />
        </ButtonGroup>
      </CardAction>
    </CardHeader>
    <CardContent class="space-y-4">
      <div>
        <Label>Path</Label>
        <p class="text-sm text-muted-foreground break-all">
          {{ props.volume.path }}
        </p>
      </div>
      <div>
        <Label>Backup Schedule</Label>
        <p class="text-sm text-muted-foreground">
          {{ props.volume.backup_cron ? props.volume.backup_cron : 'N/A' }}
        </p>
      </div>
    </CardContent>
    <CardFooter class="border-t flex gap-2">
      <VolumeBackupButton :application="props.application" :volume="props.volume" />
      <VolumeRestoreDialog
        :application="props.application"
        :volume="props.volume"
        :loadApplication="props.loadApplication"
      />
    </CardFooter>
  </Card>

  <!-- Edit Volume Dialog -->
  <Dialog v-model:open="isEditVolumeDialogOpen">
    <DialogContent class="sm:max-w-[400px]">
      <DialogHeader>
        <DialogTitle>Edit Volume</DialogTitle>
        <DialogDescription>
          {{ props.volume.name }}
        </DialogDescription>
      </DialogHeader>
      <FieldSet>
        <FieldGroup>
          <Field>
            <FieldLabel for="edit-volume-name"> Name </FieldLabel>
            <Input id="edit-volume-name" v-model="volume.name" placeholder="volume name" />
          </Field>
          <Field>
            <FieldLabel for="edit-volume-type"> Type </FieldLabel>
            <Input id="edit-volume-path" v-model="volume.path" placeholder="container path" />
          </Field>
          <Field>
            <FieldLabel for="edit-volume-backup-cron"> Backup Schedule </FieldLabel>
            <Input
              id="edit-volume-backup-cron"
              v-model="volume.backup_cron"
              placeholder="optional, e.g. 0 3 * * *"
            />
          </Field>
          <Field>
            <FieldError v-if="editVolumeErrorMessage">{{ editVolumeErrorMessage }}</FieldError>
          </Field>
        </FieldGroup>
      </FieldSet>
      <DialogFooter>
        <Button
          size="sm"
          type="button"
          variant="outline"
          @click="isEditVolumeDialogOpen = false"
          :disabled="isClickedEditVolumeConfirm"
        >
          Cancel
        </Button>
        <Button
          size="sm"
          @click="onClickEditVolumeConfirm"
          :disabled="isClickedEditVolumeConfirm || !volume.name.trim()"
        >
          <Spinner class="animate-spin" v-if="isClickedEditVolumeConfirm" />
          Save
        </Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>
