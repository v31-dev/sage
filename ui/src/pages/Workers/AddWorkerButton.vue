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
import { addWorker } from '@/services/api'

const isOpen = ref(false)
const host = ref('')
const tag = ref('')
const pool = ref('')
const errorMessage = ref('')
const isSubmitting = ref(false)

function openDialog() {
  host.value = ''
  tag.value = ''
  pool.value = ''
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
    const { hostname } = await addWorker({
      host: host.value.trim(),
      tag: tag.value.trim(),
      pool: pool.value.trim(),
    })
    isOpen.value = false
    toast.success(`Adding worker ${hostname}`)
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
            <FieldLabel for="worker-tag"> Tag (optional) </FieldLabel>
            <Input id="worker-tag" v-model="tag" placeholder="e.g. eu1" />
            <FieldDescription>
              Up to 10 letters and digits, unique to this worker. Left empty, Sage picks one.
            </FieldDescription>
          </Field>
          <Field>
            <FieldLabel for="worker-pool"> Pool (optional) </FieldLabel>
            <Input id="worker-pool" v-model="pool" placeholder="e.g. eu" />
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
          Add
        </Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>
