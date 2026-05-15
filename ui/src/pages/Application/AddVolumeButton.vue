<script setup lang="ts">
import { ref } from 'vue'
import { toast } from 'vue-sonner'
import { Plus } from 'lucide-vue-next'
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
import { Input } from '@/components/ui/input'
import { Field, FieldGroup, FieldLabel, FieldSet, FieldError } from '@/components/ui/field'
import { Spinner } from '@/components/ui/spinner'

import { type Application, type Volume, getVolumeAPI } from '@/services/api'

interface Props {
  application: Application
  volumeAPI: ReturnType<typeof getVolumeAPI>
  loadApplication: () => Promise<void>
}

const props = withDefaults(defineProps<Props>(), {})

const isAddVolumeDialogOpen = ref(false)
const volume = ref({
  name: '',
  path: '',
  backup_cron: '',
})
const addVolumeErrorMessage = ref('')
const isClickedAddVolumeConfirm = ref(false)

function openAddVolumeDialog() {
  addVolumeErrorMessage.value = ''
  volume.value = {
    name: '',
    path: '',
    backup_cron: '',
  }
  isAddVolumeDialogOpen.value = true
}

async function onClickAddVolumeConfirm() {
  addVolumeErrorMessage.value = ''

  if (!volume.value.name.trim()) {
    addVolumeErrorMessage.value = 'Please enter a volume name'
    return
  }

  if (!volume.value.path.trim()) {
    addVolumeErrorMessage.value = 'Please enter a volume path'
    return
  }

  try {
    isClickedAddVolumeConfirm.value = true
    ;(await props.volumeAPI.create(volume.value)) as Volume
    isAddVolumeDialogOpen.value = false
    await props.loadApplication()
    toast.success('Volume added successfully')
  } catch (err) {
    addVolumeErrorMessage.value = err instanceof Error ? err.message : 'Failed to add volume'
  } finally {
    isClickedAddVolumeConfirm.value = false
  }
}
</script>

<template>
  <Dialog v-model:open="isAddVolumeDialogOpen">
    <DialogTrigger asChild>
      <Button size="sm" @click="openAddVolumeDialog" class="gap-2">
        <Plus />
        Add Volume
      </Button>
    </DialogTrigger>
    <DialogContent class="sm:max-w-[400px]">
      <DialogHeader>
        <DialogTitle>Add Volume</DialogTitle>
        <DialogDescription>
          {{ props.application.name }}
        </DialogDescription>
      </DialogHeader>
      <FieldSet>
        <FieldGroup>
          <Field>
            <FieldLabel for="volume-name"> Name </FieldLabel>
            <Input id="volume-name" v-model="volume.name" placeholder="volume name" />
          </Field>
          <Field>
            <FieldLabel for="volume-path"> Path </FieldLabel>
            <Input id="volume-path" v-model="volume.path" placeholder="container path" />
          </Field>
          <Field>
            <FieldLabel for="volume-backup-cron"> Backup Schedule </FieldLabel>
            <Input
              id="volume-backup-cron"
              v-model="volume.backup_cron"
              placeholder="optional, e.g. 0 3 * * *"
            />
          </Field>
          <Field>
            <FieldError v-if="addVolumeErrorMessage">{{ addVolumeErrorMessage }}</FieldError>
          </Field>
        </FieldGroup>
      </FieldSet>
      <DialogFooter>
        <Button
          size="sm"
          type="button"
          variant="outline"
          @click="isAddVolumeDialogOpen = false"
          :disabled="isClickedAddVolumeConfirm"
        >
          Cancel
        </Button>
        <Button
          size="sm"
          @click="onClickAddVolumeConfirm"
          :disabled="isClickedAddVolumeConfirm || !volume.name.trim()"
        >
          <Spinner class="animate-spin" v-if="isClickedAddVolumeConfirm" />
          Add
        </Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>
