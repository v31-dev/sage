<script setup lang="ts">
import { ref } from 'vue'
import { toast } from 'vue-sonner'
import { Edit } from 'lucide-vue-next'
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
import { Field, FieldGroup, FieldLabel, FieldSet, FieldError } from '@/components/ui/field'
import { Input } from '@/components/ui/input'
import { Spinner } from '@/components/ui/spinner'

import { type Container, getContainerAPI } from '@/services/api'

interface Props {
  container: Container
  containersAPI: ReturnType<typeof getContainerAPI>
  loadApplication: () => Promise<void>
}

const props = withDefaults(defineProps<Props>(), {})

const isEditContainerDialogOpen = ref(false)
const domainTag = ref('')
const editContainerErrorMessage = ref('')
const isClickedEditContainerConfirm = ref(false)

function openEditContainerDialog() {
  editContainerErrorMessage.value = ''
  domainTag.value = props.container.domain_tag || ''
  isEditContainerDialogOpen.value = true
}

async function onClickEditContainerConfirm() {
  editContainerErrorMessage.value = ''

  try {
    isClickedEditContainerConfirm.value = true
    const containerData: Record<string, string | null> = {
      domain_tag: domainTag.value || null,
    }
    await props.containersAPI.update(`${props.container.worker.hostname}`, containerData)
    isEditContainerDialogOpen.value = false
    await props.loadApplication()
    toast.success('Container updated')
  } catch (err) {
    editContainerErrorMessage.value =
      err instanceof Error ? err.message : 'Failed to update container'
  } finally {
    isClickedEditContainerConfirm.value = false
  }
}
</script>

<template>
  <Dialog v-model:open="isEditContainerDialogOpen">
    <DialogTrigger asChild>
      <Button size="sm" variant="outline" @click="openEditContainerDialog" class="gap-2">
        <Edit :size="16" />
        Edit
      </Button>
    </DialogTrigger>
    <DialogContent class="sm:max-w-[400px]">
      <DialogHeader>
        <DialogTitle>Edit Container</DialogTitle>
        <DialogDescription>
          {{ props.container.worker.hostname }}
        </DialogDescription>
      </DialogHeader>
      <FieldSet>
        <FieldGroup>
          <Field>
            <FieldLabel for="domain-tag"> Domain Tag (Optional) </FieldLabel>
            <Input
              id="domain-tag"
              v-model="domainTag"
              type="text"
              placeholder="e.g. api, web, worker"
            />
          </Field>
          <Field>
            <FieldError v-if="editContainerErrorMessage">{{
              editContainerErrorMessage
            }}</FieldError>
          </Field>
        </FieldGroup>
      </FieldSet>
      <DialogFooter>
        <Button
          size="sm"
          type="button"
          variant="outline"
          @click="isEditContainerDialogOpen = false"
          :disabled="isClickedEditContainerConfirm"
        >
          Cancel
        </Button>
        <Button
          size="sm"
          @click="onClickEditContainerConfirm"
          :disabled="isClickedEditContainerConfirm"
        >
          <Spinner class="animate-spin" v-if="isClickedEditContainerConfirm" />
          Save
        </Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>
