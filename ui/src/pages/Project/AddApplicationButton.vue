<script setup lang="ts">
import { ref } from 'vue'
import { Plus } from 'lucide-vue-next'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from '@/components/ui/dialog'
import {
  Field,
  FieldError,
  FieldGroup,
  FieldLabel,
  FieldSet,
} from '@/components/ui/field'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { toast } from 'vue-sonner'
import { Spinner } from '@/components/ui/spinner'

interface Props {
  applicationAPI: any
  loadProject: () => Promise<void>
}

const props = withDefaults(defineProps<Props>(), {})

const isCreateAppDialogOpen = ref(false)
const createAppFormData = ref({
  label: '',
  description: ''
})
const createAppDialogErrorMessage = ref('')
const isClickedCreateAppConfirm = ref(false)

function openCreateAppDialog() {
  createAppFormData.value = { label: '', description: '' }
  createAppDialogErrorMessage.value = ''
  isCreateAppDialogOpen.value = true
}

async function onClickCreateAppConfirm() {
  createAppDialogErrorMessage.value = ''

  if (!createAppFormData.value.label.trim()) {
    createAppDialogErrorMessage.value = 'Application name is required'
    return
  }

  try {
    isClickedCreateAppConfirm.value = true
    await props.applicationAPI.create({
      label: createAppFormData.value.label,
      description: createAppFormData.value.description || null,
    })
    isCreateAppDialogOpen.value = false
    toast.success(`Application ${createAppFormData.value.label} created successfully`)
    await props.loadProject()
  } catch (err) {
    createAppDialogErrorMessage.value = err instanceof Error ? err.message : 'Failed to create application'
  } finally {
    isClickedCreateAppConfirm.value = false
  }
}
</script>

<template>
  <Dialog v-model:open="isCreateAppDialogOpen">
    <DialogTrigger asChild>
      <Button size="sm" @click="openCreateAppDialog" class="gap-2">
        <Plus :size="20" />
        New Application
      </Button>
    </DialogTrigger>
    <DialogContent class="sm:max-w-[600px]">
      <DialogHeader>
        <DialogTitle>Create New Application</DialogTitle>
      </DialogHeader>
      <FieldSet>
        <FieldGroup>
          <Field />
          <Field>
            <FieldLabel for="app-name">
              Name
            </FieldLabel>
            <Input id="app-name" v-model="createAppFormData.label" placeholder="required" />
          </Field>
          <Field>
            <FieldLabel for="app-description">
              Description
            </FieldLabel>
            <Textarea id="app-description" v-model="createAppFormData.description" class="resize-none"
              placeholder="optional" />
          </Field>
          <Field>
            <FieldError v-if="createAppDialogErrorMessage">{{ createAppDialogErrorMessage }}</FieldError>
          </Field>
        </FieldGroup>
      </FieldSet>
      <DialogFooter>
        <Button size="sm" type="button" variant="outline" @click="isCreateAppDialogOpen = false"
          :disabled="isClickedCreateAppConfirm">
          Cancel
        </Button>
        <Button size="sm" @click="onClickCreateAppConfirm" :disabled="isClickedCreateAppConfirm">
          <Spinner class="animate-spin" v-if="isClickedCreateAppConfirm" />
          Create
        </Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>
