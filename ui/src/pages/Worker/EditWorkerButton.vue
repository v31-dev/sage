<script setup lang="ts">
import { ref } from 'vue'
import { toast } from 'vue-sonner'
import { Edit } from 'lucide-vue-next'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from '@/components/ui/dialog'
import {
  Field,
  FieldDescription,
  FieldError,
  FieldGroup,
  FieldLabel,
  FieldSet,
} from '@/components/ui/field'
import { Input } from '@/components/ui/input'
import { Spinner } from '@/components/ui/spinner'
import { type Worker, updateWorker } from '@/services/api'

interface Props {
  worker: Worker
  loadWorkerStatus: () => Promise<void>
}

const props = defineProps<Props>()

const isOpen = ref(false)
const tag = ref('')
const pool = ref('')
const errorMessage = ref('')
const isSubmitting = ref(false)

function openDialog() {
  tag.value = props.worker.tag ?? ''
  pool.value = props.worker.pool ?? ''
  errorMessage.value = ''
  isOpen.value = true
}

async function onConfirm() {
  errorMessage.value = ''
  try {
    isSubmitting.value = true
    await updateWorker(props.worker.hostname, { tag: tag.value.trim(), pool: pool.value.trim() })
    isOpen.value = false
    await props.loadWorkerStatus()
    toast.success(`Worker ${props.worker.hostname} updated`)
  } catch (err) {
    errorMessage.value = err instanceof Error ? err.message : 'Failed to update worker'
  } finally {
    isSubmitting.value = false
  }
}
</script>

<template>
  <Dialog v-model:open="isOpen">
    <DialogTrigger asChild>
      <Button size="sm" variant="outline" class="gap-2" @click="openDialog">
        <Edit :size="16" />
        Edit
      </Button>
    </DialogTrigger>
    <DialogContent class="sm:max-w-[500px]">
      <DialogHeader>
        <DialogTitle>Edit Worker</DialogTitle>
        <DialogDescription>{{ props.worker.hostname }}</DialogDescription>
      </DialogHeader>
      <FieldSet>
        <FieldGroup>
          <Field>
            <FieldLabel for="worker-edit-tag"> Tag </FieldLabel>
            <Input id="worker-edit-tag" v-model="tag" placeholder="e.g. eu1" />
            <FieldDescription>Up to 10 letters and digits, unique to this worker.</FieldDescription>
          </Field>
          <Field>
            <FieldLabel for="worker-edit-pool"> Pool (optional) </FieldLabel>
            <Input id="worker-edit-pool" v-model="pool" placeholder="e.g. eu" />
            <FieldDescription>A group this worker belongs to, shared with others.</FieldDescription>
          </Field>
          <Field>
            <FieldError v-if="errorMessage">{{ errorMessage }}</FieldError>
          </Field>
        </FieldGroup>
      </FieldSet>
      <DialogFooter>
        <Button
          size="sm"
          type="button"
          variant="outline"
          :disabled="isSubmitting"
          @click="isOpen = false"
        >
          Cancel
        </Button>
        <Button size="sm" :disabled="isSubmitting" @click="onConfirm">
          <Spinner class="animate-spin" v-if="isSubmitting" />
          Save
        </Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>
