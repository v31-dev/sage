<script setup lang="ts">
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { toast } from 'vue-sonner'
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
  FieldSet, 
  FieldError 
} from '@/components/ui/field'
import { Spinner } from '@/components/ui/spinner'

import { 
    type Application, 
    getApplicationAPI
 } from '@/services/api'


interface Props {
  application: Application,
  applicationAPI: ReturnType<typeof getApplicationAPI>
}

const props = withDefaults(defineProps<Props>(), {})

const router = useRouter()

const isDeleteDialogOpen = ref(false)
const isClickedDeleteConfirm = ref(false)
const deleteDialogErrorMessage = ref('')

function openDeleteApplicationDialog() {
  isDeleteDialogOpen.value = true
  isClickedDeleteConfirm.value = false
  deleteDialogErrorMessage.value = ''
}

async function onClickDeleteApplicationConfirm() {
  isClickedDeleteConfirm.value = true

  await new Promise(resolve => setTimeout(resolve, 1000))

  try {
    await props.applicationAPI.delete(props.application.name)
    isDeleteDialogOpen.value = false
    toast.success(`Application ${props.application.name} deleted successfully`)
    router.push(`/projects/${props.application.project.name}`)
  } catch (err) {
    deleteDialogErrorMessage.value = err instanceof Error ? err.message : 'Failed to delete application'
  } finally {
    isClickedDeleteConfirm.value = false
  }
}
</script>

<template>
  <Dialog v-model:open="isDeleteDialogOpen">
    <DialogTrigger asChild>
      <Button @click="openDeleteApplicationDialog" variant="destructive">Delete</Button>
    </DialogTrigger>
    <DialogContent class="sm:max-w-[600px]">
      <DialogHeader>
        <DialogTitle>Delete Application</DialogTitle>
        <DialogDescription>
          {{ props.application.name }}
        </DialogDescription>
      </DialogHeader>
      <FieldSet>
        <FieldGroup>
          <Field />
          <Field>
            <p class="text-sm text-muted-foreground">
              Are you sure you want to delete this application? This action cannot be undone.
            </p>
          </Field>
          <Field>
            <FieldError v-if="deleteDialogErrorMessage">{{ deleteDialogErrorMessage }}
            </FieldError>
          </Field>
        </FieldGroup>
      </FieldSet>
      <DialogFooter>
        <Button variant="destructive" @click="onClickDeleteApplicationConfirm"
          :disabled="isClickedDeleteConfirm">
          <Spinner class="animate-spin" v-if="isClickedDeleteConfirm" />
          Delete
        </Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>
