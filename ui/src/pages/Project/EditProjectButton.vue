<script setup lang="ts">
import { ref } from 'vue'
import { Edit } from 'lucide-vue-next'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from '@/components/ui/dialog'
import { Field, FieldError, FieldGroup, FieldLabel, FieldSet } from '@/components/ui/field'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { toast } from 'vue-sonner'
import { Spinner } from '@/components/ui/spinner'

import { type Project } from '@/services/api'
import CustomInput from '@/components/CustomInput.vue'

interface Props {
  project: Project
  projectAPI: any
  loadProject: () => Promise<void>
}

const props = withDefaults(defineProps<Props>(), {})

const isEditDialogOpen = ref(false)
const editFormData = ref({
  label: '',
  description: '',
  env: '',
})
const editDialogErrorMessage = ref('')
const isClickedEditConfirm = ref(false)

function openEditDialog() {
  editFormData.value.label = props.project.label || ''
  editFormData.value.description = props.project.description || ''
  editFormData.value.env = props.project.env || ''
  editDialogErrorMessage.value = ''
  isEditDialogOpen.value = true
}

async function handleUpdateProject() {
  editDialogErrorMessage.value = ''

  try {
    isClickedEditConfirm.value = true
    await props.projectAPI.update(props.project.name, {
      label: editFormData.value.label || null,
      description: editFormData.value.description || null,
      env: editFormData.value.env || null,
    })
    isEditDialogOpen.value = false
    toast.success('Project updated successfully')
    await props.loadProject()
  } catch (err) {
    editDialogErrorMessage.value = err instanceof Error ? err.message : 'Failed to update project'
  } finally {
    isClickedEditConfirm.value = false
  }
}
</script>

<template>
  <Dialog v-model:open="isEditDialogOpen">
    <DialogTrigger asChild>
      <Button size="sm" variant="outline" @click="openEditDialog">
        <Edit />
        Edit
      </Button>
    </DialogTrigger>
    <DialogContent class="sm:max-w-[600px]">
      <DialogHeader>
        <DialogTitle>Edit Project</DialogTitle>
      </DialogHeader>
      <FieldSet>
        <FieldGroup>
          <Field>
            <FieldLabel for="label"> Name </FieldLabel>
            <Input id="label" v-model="editFormData.label" placeholder="optional" />
          </Field>
          <Field>
            <FieldLabel for="description"> Description </FieldLabel>
            <Textarea
              id="description"
              v-model="editFormData.description"
              class="resize-none"
              placeholder="optional"
            />
          </Field>
          <Field>
            <FieldLabel for="env"> Environment Variables </FieldLabel>
            <CustomInput
              id="env"
              type="textarea"
              tooltip="KEY=VALUE pairs, one per line\nComments with # are allowed\nVALUE can be string quoted"
              secret
              v-model="editFormData.env"
              class="resize-none"
              placeholder="optional"
            />
          </Field>
          <Field>
            <FieldError v-if="editDialogErrorMessage">
              {{ editDialogErrorMessage }}
            </FieldError>
          </Field>
        </FieldGroup>
      </FieldSet>
      <DialogFooter>
        <Button
          size="sm"
          type="button"
          variant="outline"
          @click="isEditDialogOpen = false"
          :disabled="isClickedEditConfirm"
        >
          Cancel
        </Button>
        <Button size="sm" @click="handleUpdateProject" :disabled="isClickedEditConfirm">
          <Spinner class="animate-spin" v-if="isClickedEditConfirm" />
          Save
        </Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>
