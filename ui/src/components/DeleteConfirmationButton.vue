<script setup lang="ts">
import { ref } from 'vue'
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
import { Trash } from 'lucide-vue-next'


interface Props {
  title: string,
  description: string,
  onConfirm: () => Promise<void>,
}

const props = withDefaults(defineProps<Props>(), {})

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
    await props.onConfirm()
    isDeleteDialogOpen.value = false
  } catch (err) {
    deleteDialogErrorMessage.value = err instanceof Error ? err.message : `Failed to delete ${props.title.toLowerCase()}`
  } finally {
    isClickedDeleteConfirm.value = false
  }
}
</script>

<template>
  <Dialog v-model:open="isDeleteDialogOpen">
    <DialogTrigger asChild>
      <Button @click="openDeleteApplicationDialog" variant="destructive" size="sm">
        <Trash />Delete
      </Button>
    </DialogTrigger>
    <DialogContent class="sm:max-w-[600px]">
      <DialogHeader>
        <DialogTitle>Delete {{ props.title }}</DialogTitle>
        <DialogDescription>
          {{ props.description }}
        </DialogDescription>
      </DialogHeader>
      <FieldSet>
        <FieldGroup>
          <Field>
            <p class="text-sm text-muted-foreground">
              Are you sure you want to delete this {{ props.title.toLowerCase() }}? This action cannot be undone.
            </p>
          </Field>
          <Field>
            <FieldError v-if="deleteDialogErrorMessage">{{ deleteDialogErrorMessage }}
            </FieldError>
          </Field>
        </FieldGroup>
      </FieldSet>
      <DialogFooter>
        <Button size="sm" variant="destructive" @click="onClickDeleteApplicationConfirm" :disabled="isClickedDeleteConfirm">
          <Spinner class="animate-spin" v-if="isClickedDeleteConfirm" />
          Delete
        </Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>