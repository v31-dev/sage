<script setup lang="ts">
import { computed, ref } from 'vue'
import { toast } from 'vue-sonner'
import { Plus } from 'lucide-vue-next'
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
import { Field, FieldGroup, FieldLabel, FieldSet, FieldError } from '@/components/ui/field'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { Input } from '@/components/ui/input'
import { Spinner } from '@/components/ui/spinner'

import { type Application, createContainer } from '@/services/api'
import { useAppStore } from '@/stores/app'

interface Props {
  application: Application
  loadApplication: () => Promise<void>
}

const props = withDefaults(defineProps<Props>(), {})
const appStore = useAppStore()

const isAddContainerDialogOpen = ref(false)
// The workers in the app store's list that hold no container of the application.
const availableWorkers = computed(() =>
  (appStore.workers ?? []).filter(
    worker =>
      !props.application.containers.some(container => container.worker.hostname === worker.hostname)
  )
)
const selectedWorker = ref('')
const domainTag = ref('')
const addContainerErrorMessage = ref('')
const isClickedAddContainerConfirm = ref(false)

function openAddContainerDialog() {
  addContainerErrorMessage.value = appStore.workersError ? 'Failed to load available workers' : ''
  selectedWorker.value = availableWorkers.value[0]?.hostname ?? ''
  domainTag.value = ''
  isAddContainerDialogOpen.value = true
}

async function onClickAddContainerConfirm() {
  addContainerErrorMessage.value = ''

  if (!selectedWorker.value) {
    addContainerErrorMessage.value = 'Please select a worker'
    return
  }

  try {
    isClickedAddContainerConfirm.value = true
    const containerData: Record<string, string> = { worker: selectedWorker.value }
    if (domainTag.value) {
      containerData.domain_tag = domainTag.value
    }
    await createContainer(props.application.project.name, props.application.name, containerData)
    isAddContainerDialogOpen.value = false
    await props.loadApplication()
    toast.success('Container added')
  } catch (err) {
    addContainerErrorMessage.value = err instanceof Error ? err.message : 'Failed to add container'
  } finally {
    isClickedAddContainerConfirm.value = false
  }
}
</script>

<template>
  <Dialog v-model:open="isAddContainerDialogOpen">
    <DialogTrigger asChild>
      <Button size="sm" @click="openAddContainerDialog" class="gap-2">
        <Plus :size="20" />
        New Container
      </Button>
    </DialogTrigger>
    <DialogContent class="sm:max-w-[400px]">
      <DialogHeader>
        <DialogTitle>Add Container</DialogTitle>
        <DialogDescription>
          {{ props.application.name }}
        </DialogDescription>
      </DialogHeader>
      <FieldSet>
        <FieldGroup>
          <Field />
          <Field>
            <FieldLabel for="worker-select"> Select Worker </FieldLabel>
            <Select v-model="selectedWorker" :disabled="availableWorkers.length === 0">
              <SelectTrigger id="worker-select">
                <SelectValue
                  :placeholder="
                    availableWorkers.length === 0 ? 'No workers available' : 'Choose a worker...'
                  "
                />
              </SelectTrigger>
              <SelectContent>
                <SelectItem
                  v-for="worker in availableWorkers"
                  :key="worker.hostname"
                  :value="worker.hostname"
                >
                  {{ worker.hostname }}
                </SelectItem>
              </SelectContent>
            </Select>
          </Field>
          <Field>
            <FieldLabel for="domain-tag"> Domain Tag (Optional) </FieldLabel>
            <Input
              id="domain-tag"
              v-model="domainTag"
              type="text"
              placeholder="e.g. api, web, worker"
            />
          </Field>
          <Field>
            <FieldError v-if="addContainerErrorMessage">{{ addContainerErrorMessage }}</FieldError>
          </Field>
        </FieldGroup>
      </FieldSet>
      <DialogFooter>
        <Button
          size="sm"
          type="button"
          variant="outline"
          @click="isAddContainerDialogOpen = false"
          :disabled="isClickedAddContainerConfirm"
        >
          Cancel
        </Button>
        <Button
          size="sm"
          @click="onClickAddContainerConfirm"
          :disabled="isClickedAddContainerConfirm || !selectedWorker"
        >
          <Spinner class="animate-spin" v-if="isClickedAddContainerConfirm" />
          Add
        </Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>
