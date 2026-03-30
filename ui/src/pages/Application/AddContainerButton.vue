<script setup lang="ts">
import { ref } from 'vue'
import { toast } from 'vue-sonner'
import { Plus } from 'lucide-vue-next'
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
  FieldLabel, 
  FieldSet, 
  FieldError 
} from '@/components/ui/field'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { Spinner } from '@/components/ui/spinner'

import { 
    type Application,
    type Worker,
    type Container,
    getApplicationAPI,
    getContainerAPI
 } from '@/services/api'


interface Props {
  application: Application,
  applicationAPI: ReturnType<typeof getApplicationAPI>,
  containersAPI: ReturnType<typeof getContainerAPI>,
  loadApplication: () => Promise<void>
}

const props = withDefaults(defineProps<Props>(), {})

const isAddContainerDialogOpen = ref(false)
const availableWorkers = ref<Worker[]>([])
const selectedWorker = ref('')
const addContainerErrorMessage = ref('')
const isClickedAddContainerConfirm = ref(false)

function openAddContainerDialog() {
  addContainerErrorMessage.value = ''
  selectedWorker.value = ''
  loadAvailableWorkers()
  isAddContainerDialogOpen.value = true
}

async function loadAvailableWorkers() {
  try {
    availableWorkers.value = await props.applicationAPI.action(`${props.application.name}/get_available_workers`) as Worker[]
    if (availableWorkers.value.length > 0) {
      selectedWorker.value = availableWorkers.value[0]?.hostname || ''
    }
  } catch (err) {
    console.error('Failed to load available workers:', err)
    addContainerErrorMessage.value = 'Failed to load available workers'
  }
}

async function onClickAddContainerConfirm() {
  addContainerErrorMessage.value = ''

  if (!selectedWorker.value) {
    addContainerErrorMessage.value = 'Please select a worker'
    return
  }

  try {
    isClickedAddContainerConfirm.value = true
    await props.containersAPI.create({ worker: selectedWorker.value }) as Container
    isAddContainerDialogOpen.value = false
    await props.loadApplication()
    toast.success('Container added successfully')
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
            <FieldLabel for="worker-select">
              Select Worker
            </FieldLabel>
            <Select v-model="selectedWorker" :disabled="availableWorkers.length === 0">
              <SelectTrigger id="worker-select">
                <SelectValue
                  :placeholder="availableWorkers.length === 0 ? 'No workers available' : 'Choose a worker...'" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem v-for="worker in availableWorkers" :key="worker.hostname"
                  :value="worker.hostname">
                  {{ worker.hostname }}
                </SelectItem>
              </SelectContent>
            </Select>
          </Field>
          <Field>
            <FieldError v-if="addContainerErrorMessage">{{ addContainerErrorMessage }}</FieldError>
          </Field>
        </FieldGroup>
      </FieldSet>
      <DialogFooter>
        <Button size="sm" type="button" variant="outline" @click="isAddContainerDialogOpen = false"
          :disabled="isClickedAddContainerConfirm">
          Cancel
        </Button>
        <Button size="sm" @click="onClickAddContainerConfirm"
          :disabled="isClickedAddContainerConfirm || !selectedWorker">
          <Spinner class="animate-spin" v-if="isClickedAddContainerConfirm" />
          Add
        </Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>
