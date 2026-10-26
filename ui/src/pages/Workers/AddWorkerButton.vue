<script setup lang="ts">
import { ref } from 'vue'
import { Plus } from 'lucide-vue-next'
import { toast } from 'vue-sonner'
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
import { Field, FieldError, FieldGroup, FieldLabel, FieldSet } from '@/components/ui/field'
import { Input } from '@/components/ui/input'
import { Spinner } from '@/components/ui/spinner'
import { addWorker } from '@/services/api'

interface Props {
  loadWorkers: () => Promise<void>
}

const props = withDefaults(defineProps<Props>(), {})

const isOpen = ref(false)
const host = ref('')
const errorMessage = ref('')
const isSubmitting = ref(false)

function openDialog() {
  host.value = ''
  errorMessage.value = ''
  isOpen.value = true
}

async function onConfirm() {
  errorMessage.value = ''

  if (!host.value.trim()) {
    errorMessage.value = 'Hostname or IP is required'
    return
  }

  try {
    isSubmitting.value = true
    const { hostname } = await addWorker(host.value.trim())
    isOpen.value = false
    toast.success(`Adding worker ${hostname}`)
    await props.loadWorkers()
  } catch (err) {
    errorMessage.value = err instanceof Error ? err.message : 'Failed to add worker'
  } finally {
    isSubmitting.value = false
  }
}
</script>

<template>
  <Dialog v-model:open="isOpen">
    <DialogTrigger asChild>
      <Button size="sm" class="gap-2" @click="openDialog">
        <Plus :size="20" />
        Add Worker
      </Button>
    </DialogTrigger>
    <DialogContent class="sm:max-w-[500px]">
      <DialogHeader>
        <DialogTitle>Add Worker</DialogTitle>
        <DialogDescription>
          A machine on your tailnet that Sage can reach over SSH, with Docker installed. One that
          another Sage manages is refused: delete it from that Sage first.
        </DialogDescription>
      </DialogHeader>
      <FieldSet>
        <FieldGroup>
          <Field>
            <FieldLabel for="worker-host"> Hostname or Tailscale IP </FieldLabel>
            <Input id="worker-host" v-model="host" placeholder="e.g. worker-1 or 100.64.0.2" />
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
          Add
        </Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>
