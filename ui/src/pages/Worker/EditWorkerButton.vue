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
const subdomain = ref('')
const errorMessage = ref('')
const isSubmitting = ref(false)

function openDialog() {
  subdomain.value = props.worker.subdomain ?? ''
  errorMessage.value = ''
  isOpen.value = true
}

async function onConfirm() {
  errorMessage.value = ''
  try {
    isSubmitting.value = true
    await updateWorker(props.worker.hostname, { subdomain: subdomain.value.trim() })
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
            <FieldLabel for="worker-edit-subdomain"> Subdomain </FieldLabel>
            <Input id="worker-edit-subdomain" v-model="subdomain" placeholder="e.g. eu1" />
            <FieldDescription>
              The worker's internal names are under it. Lowercase letters, digits and dashes, unique
              to this worker.
            </FieldDescription>
            <FieldDescription>
              ⚠ A new subdomain changes the worker's internal names and reissues the workers'
              certificate. Let's Encrypt issues at most 5 for the same names and 50 for the domain
              every 7 days.
            </FieldDescription>
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
