<script setup lang="ts">
import { computed, ref } from 'vue'
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
import { Field, FieldGroup, FieldSet, FieldError } from '@/components/ui/field'
import { Spinner } from '@/components/ui/spinner'
import { Trash, Check, type LucideIcon } from 'lucide-vue-next'

interface Props {
  title: string
  description: string
  mode: 'delete' | 'info'
  icon?: LucideIcon
  buttonText?: string
  onConfirm: () => Promise<void>
}

const props = withDefaults(defineProps<Props>(), {})

const modeText = computed(() => {
  switch (props.mode) {
    case 'delete':
      return 'Delete'
    case 'info':
      return 'Confirm'
    default:
      return 'OK'
  }
})
const modeIcon = computed(() => {
  if (props.icon) {
    return props.icon
  }
  switch (props.mode) {
    case 'delete':
      return Trash
    case 'info':
      return Check
    default:
      return null
  }
})
const isDialogOpen = ref(false)
const isClickedConfirm = ref(false)
const dialogErrorMessage = ref('')

function openDialog() {
  isDialogOpen.value = true
  isClickedConfirm.value = false
  dialogErrorMessage.value = ''
}

async function onClickConfirm() {
  isClickedConfirm.value = true

  await new Promise(resolve => setTimeout(resolve, 1000))

  try {
    await props.onConfirm()
    isDialogOpen.value = false
  } catch (err) {
    dialogErrorMessage.value =
      err instanceof Error ? err.message : `Failed to delete ${props.title.toLowerCase()}`
  } finally {
    isClickedConfirm.value = false
  }
}
</script>

<template>
  <Dialog v-model:open="isDialogOpen">
    <DialogTrigger asChild>
      <Button
        @click="openDialog"
        :variant="props.mode === 'delete' ? 'destructive' : 'outline'"
        size="sm"
      >
        <component :is="modeIcon" />{{ props.buttonText || modeText }}
      </Button>
    </DialogTrigger>
    <DialogContent class="sm:max-w-[600px]">
      <DialogHeader>
        <DialogTitle>{{ modeText }} {{ props.title }}</DialogTitle>
        <DialogDescription>
          {{ props.description }}
        </DialogDescription>
      </DialogHeader>
      <FieldSet>
        <FieldGroup>
          <Field>
            <p class="text-sm text-muted-foreground">
              Are you sure you want to {{ modeText.toLowerCase() }} this
              {{ props.title.toLowerCase() }}? This action cannot be undone.
            </p>
          </Field>
          <Field>
            <FieldError v-if="dialogErrorMessage">{{ dialogErrorMessage }} </FieldError>
          </Field>
        </FieldGroup>
      </FieldSet>
      <DialogFooter>
        <Button
          size="sm"
          :variant="props.mode === 'delete' ? 'destructive' : 'default'"
          @click="onClickConfirm"
          :disabled="isClickedConfirm"
        >
          <Spinner class="animate-spin" v-if="isClickedConfirm" />
          {{ modeText }}
        </Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>
