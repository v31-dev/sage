<script setup lang="ts">
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { Trash } from 'lucide-vue-next'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from '@/components/ui/dialog'
import { Field, FieldError, FieldGroup, FieldSet } from '@/components/ui/field'
import { toast } from 'vue-sonner'
import { Spinner } from '@/components/ui/spinner'

import { type Project } from '@/services/api'

interface Props {
  project: Project
  projectAPI: any
}

const props = withDefaults(defineProps<Props>(), {})

const router = useRouter()

const isDeleteDialogOpen = ref(false)
const isClickedDeleteConfirm = ref(false)
const deleteDialogErrorMessage = ref('')

function openDeleteProjectDialog() {
  isDeleteDialogOpen.value = true
  isClickedDeleteConfirm.value = false
  deleteDialogErrorMessage.value = ''
}

async function onClickDeleteProjectConfirm() {
  isClickedDeleteConfirm.value = true

  await new Promise(resolve => setTimeout(resolve, 1000))

  try {
    await props.projectAPI.delete(props.project.name)
    isDeleteDialogOpen.value = false
    toast.success(`Project ${props.project.name} deleted successfully`)
    router.push('/projects')
  } catch (err) {
    deleteDialogErrorMessage.value = err instanceof Error ? err.message : 'Failed to delete project'
  } finally {
    isClickedDeleteConfirm.value = false
  }
}
</script>

<template>
  <Dialog v-model:open="isDeleteDialogOpen">
    <DialogTrigger asChild>
      <Button size="sm" variant="destructive" @click="openDeleteProjectDialog">
        <Trash />
        Delete
      </Button>
    </DialogTrigger>
    <DialogContent class="sm:max-w-[600px]">
      <DialogHeader>
        <DialogTitle>Delete Project</DialogTitle>
      </DialogHeader>
      <FieldSet>
        <FieldGroup>
          <Field />
          <Field>
            <p class="text-sm text-muted-foreground">
              Are you sure you want to delete this project? This action cannot be undone.
            </p>
          </Field>
          <Field>
            <FieldError v-if="deleteDialogErrorMessage">
              {{ deleteDialogErrorMessage }}
            </FieldError>
          </Field>
        </FieldGroup>
      </FieldSet>
      <DialogFooter>
        <Button
          size="sm"
          variant="destructive"
          @click="onClickDeleteProjectConfirm"
          :disabled="isClickedDeleteConfirm"
        >
          <Spinner class="animate-spin" v-if="isClickedDeleteConfirm" />
          Delete
        </Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>
