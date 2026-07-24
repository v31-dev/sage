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
import { type LucideIcon } from 'lucide-vue-next'

interface Props {
  triggerText: string
  title: string
  body: string
  description?: string
  confirmText?: string
  destructive?: boolean
  icon?: LucideIcon
  disabled?: boolean
  loading?: boolean
  triggerClass?: string
  onConfirm: () => Promise<void>
}

const props = withDefaults(defineProps<Props>(), {
  destructive: false,
  disabled: false,
  loading: false,
})

const confirmLabel = computed(() => props.confirmText ?? props.triggerText)

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
    dialogErrorMessage.value = err instanceof Error ? err.message : `${confirmLabel.value} failed`
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
        :variant="destructive ? 'destructive' : 'outline'"
        size="sm"
        :disabled="disabled"
        :class="triggerClass"
      >
        <Spinner class="animate-spin" v-if="loading" />
        <component :is="icon" v-if="icon" />{{ triggerText }}
      </Button>
    </DialogTrigger>
    <DialogContent class="sm:max-w-[600px]">
      <DialogHeader>
        <DialogTitle>{{ title }}</DialogTitle>
        <DialogDescription v-if="description">
          {{ description }}
        </DialogDescription>
      </DialogHeader>
      <FieldSet>
        <FieldGroup>
          <Field>
            <p class="text-sm text-muted-foreground">{{ body }}</p>
          </Field>
          <Field>
            <FieldError v-if="dialogErrorMessage">{{ dialogErrorMessage }} </FieldError>
          </Field>
        </FieldGroup>
      </FieldSet>
      <DialogFooter>
        <Button
          size="sm"
          :variant="destructive ? 'destructive' : 'default'"
          @click="onClickConfirm"
          :disabled="isClickedConfirm"
        >
          <Spinner class="animate-spin" v-if="isClickedConfirm" />
          {{ confirmLabel }}
        </Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>
