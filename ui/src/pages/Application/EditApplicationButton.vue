<script setup lang="ts">
import { ref } from 'vue'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { 
  Dialog, 
  DialogContent, 
  DialogDescription, 
  DialogFooter, 
  DialogHeader, 
  DialogTitle, 
  DialogTrigger 
} from '@/components/ui/dialog'
import { Button } from '@/components/ui/button'
import { 
  Field, 
  FieldGroup, 
  FieldLabel, 
  FieldSet, 
  FieldError 
} from '@/components/ui/field'
import { Spinner } from '@/components/ui/spinner'
import { toast } from 'vue-sonner'

import { 
    type Application, 
    getApplicationAPI
 } from '@/services/api'


interface Props {
  application: Application,
  applicationAPI: ReturnType<typeof getApplicationAPI>,
  loadApplication: () => Promise<void>
}

const props = withDefaults(defineProps<Props>(), {})

const isEditDialogOpen = ref(false)
const editFormData = ref({
  label: '',
  description: '',
  image: '',
  env: '',
  args: '',
})
const editDialogErrorMessage = ref('')
const isClickedEditConfirm = ref(false)

function openEditDialog() {
  editFormData.value.label = props.application.label || ''
  editFormData.value.description = props.application.description || ''
  editFormData.value.image = props.application.image || ''
  editFormData.value.env = props.application.env || ''
  editFormData.value.args = props.application.args || ''
  editDialogErrorMessage.value = ''
  isEditDialogOpen.value = true
}

async function handleUpdateApplication() {
  editDialogErrorMessage.value = ''

  try {
    isClickedEditConfirm.value = true
    await props.applicationAPI.update(props.application.name, {
      label: editFormData.value.label || null,
      description: editFormData.value.description || null,
      image: editFormData.value.image || null,
      env: editFormData.value.env || null,
      args: editFormData.value.args || null,
    })
    await props.loadApplication()
    isEditDialogOpen.value = false
    toast.success('Application updated successfully')
  } catch (err) {
    editDialogErrorMessage.value = err instanceof Error ? err.message : 'Failed to update application'
  } finally {
    isClickedEditConfirm.value = false
  }
}
</script>

<template>
  <Dialog v-model:open="isEditDialogOpen">
    <DialogTrigger asChild>
      <Button @click="openEditDialog">Edit</Button>
    </DialogTrigger>
    <DialogContent class="sm:max-w-[600px]">
      <DialogHeader>
        <DialogTitle>Edit Application</DialogTitle>
        <DialogDescription>{{ application.name }}</DialogDescription>
      </DialogHeader>
      <FieldSet>
        <FieldGroup>
          <Field />
          <Field>
            <FieldLabel for="label">
              Name
            </FieldLabel>
            <Input id="label" v-model="editFormData.label" placeholder="required" />
          </Field>
          <Field>
            <FieldLabel for="description">
              Description
            </FieldLabel>
            <Textarea id="description" v-model="editFormData.description" class="resize-none" placeholder="optional" />
          </Field>
          <Field>
            <FieldLabel for="image">
              Image
            </FieldLabel>
            <Input id="image" v-model="editFormData.image" placeholder="optional" />
          </Field>
          <Field>
            <FieldLabel for="env">
              Environment Variables
            </FieldLabel>
            <Textarea id="env" v-model="editFormData.env" class="resize-none" placeholder="optional" />
          </Field>
          <Field>
            <FieldLabel for="args">
              Build Arguments
            </FieldLabel>
            <Textarea id="args" v-model="editFormData.args" class="resize-none" placeholder="optional" />
          </Field>
          <Field>
            <FieldError v-if="editDialogErrorMessage">{{ editDialogErrorMessage }}</FieldError>
          </Field>
        </FieldGroup>
      </FieldSet>
      <DialogFooter>
        <Button type="button" variant="outline" @click="isEditDialogOpen = false" :disabled="isClickedEditConfirm">
          Cancel
        </Button>
        <Button @click="handleUpdateApplication" :disabled="isClickedEditConfirm">
          <Spinner class="animate-spin" v-if="isClickedEditConfirm" />
          Save
        </Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>