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
  DialogTrigger,
} from '@/components/ui/dialog'
import { Button } from '@/components/ui/button'
import { Field, FieldGroup, FieldLabel, FieldSet, FieldError } from '@/components/ui/field'
import { Spinner } from '@/components/ui/spinner'
import { toast } from 'vue-sonner'
import {
  Select,
  SelectItem,
  SelectContent,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import CustomInput from '@/components/CustomInput.vue'

import { type Application, getApplicationAPI } from '@/services/api'
import { Edit } from 'lucide-vue-next'

interface Props {
  application: Application
  applicationAPI: ReturnType<typeof getApplicationAPI>
  loadApplication: () => Promise<void>
}

const props = withDefaults(defineProps<Props>(), {})

const isEditDialogOpen = ref(false)
const editFormData = ref({
  label: '',
  description: '',
  type: 'docker',
  image: '',
  repo: '',
  path: 'Dockerfile',
  env: '',
  args: '',
})
const editDialogErrorMessage = ref('')
const isClickedEditConfirm = ref(false)

function openEditDialog() {
  editFormData.value.label = props.application.label || ''
  editFormData.value.description = props.application.description || ''
  editFormData.value.type = props.application.type || 'docker'
  editFormData.value.image = props.application.image || ''
  editFormData.value.repo = props.application.repo || ''
  editFormData.value.path = props.application.path || 'Dockerfile'
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
      type: editFormData.value.type || null,
      image: editFormData.value.image || null,
      repo: editFormData.value.repo || null,
      path: editFormData.value.path || null,
      env: editFormData.value.env || null,
      args: editFormData.value.args || null,
    })
    await props.loadApplication()
    isEditDialogOpen.value = false
    toast.success('Application updated successfully')
  } catch (err) {
    editDialogErrorMessage.value =
      err instanceof Error ? err.message : 'Failed to update application'
  } finally {
    isClickedEditConfirm.value = false
  }
}
</script>

<template>
  <Dialog v-model:open="isEditDialogOpen">
    <DialogTrigger asChild>
      <Button @click="openEditDialog" variant="outline" size="sm"> <Edit />Edit </Button>
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
            <FieldLabel for="label"> Name </FieldLabel>
            <Input id="label" v-model="editFormData.label" placeholder="required" />
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
            <FieldLabel for="type"> Type </FieldLabel>
            <Select id="type" v-model="editFormData.type" placeholder="required">
              <SelectTrigger>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="docker" key="docker">Docker</SelectItem>
                <SelectItem value="git" key="git">Git</SelectItem>
              </SelectContent>
            </Select>
          </Field>
          <Field v-if="editFormData.type == 'docker'">
            <FieldLabel for="image"> Image </FieldLabel>
            <Input id="image" v-model="editFormData.image" placeholder="required" />
          </Field>
          <Field v-if="editFormData.type == 'git'">
            <FieldLabel for="repo"> Repository </FieldLabel>
            <CustomInput
              id="repo"
              v-model="editFormData.repo"
              placeholder="required"
              tooltip="https://github.com/user/repo.git<?#branch><?:sub-directory>"
            />
          </Field>
          <Field v-if="editFormData.type == 'git'">
            <FieldLabel for="path"> Path </FieldLabel>
            <CustomInput
              id="path"
              v-model="editFormData.path"
              placeholder="required"
              tooltip="Dockerfile"
            />
          </Field>
          <Field>
            <FieldLabel for="env"> Environment Variables </FieldLabel>
            <Textarea
              id="env"
              v-model="editFormData.env"
              class="resize-none"
              placeholder="optional"
            />
          </Field>
          <Field v-if="editFormData.type == 'git'">
            <FieldLabel for="args"> Build Arguments </FieldLabel>
            <Textarea
              id="args"
              v-model="editFormData.args"
              class="resize-none"
              placeholder="optional"
            />
          </Field>
          <Field>
            <FieldError v-if="editDialogErrorMessage">{{ editDialogErrorMessage }}</FieldError>
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
        <Button size="sm" @click="handleUpdateApplication" :disabled="isClickedEditConfirm">
          <Spinner class="animate-spin" v-if="isClickedEditConfirm" />
          Save
        </Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>
