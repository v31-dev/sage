<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Plus } from 'lucide-vue-next'
import {
  Card,
  CardAction,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle
} from '@/components/ui/card'
import { ButtonGroup } from '@/components/ui/button-group'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from '@/components/ui/dialog'
import {
  Field,
  FieldError,
  FieldGroup,
  FieldLabel,
  FieldSet,
} from '@/components/ui/field'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { toast } from 'vue-sonner'
import { Spinner } from '@/components/ui/spinner'
import { Label } from '@/components/ui/label'
import { Badge } from '@/components/ui/badge'

import { getApplicationAPI, getContainerAPI, type Application, type Worker, type Container } from '@/services/api'

const route = useRoute()
const router = useRouter()
const projectName = route.params.projectId as string
const appName = route.params.appId as string

const applicationAPI = getApplicationAPI(projectName)
const containersAPI = getContainerAPI(projectName, appName)
const application = ref<Application | null>(null)
const isLoading = ref(true)

onMounted(async () => {
  await loadApplication()
})

async function loadApplication() {
  try {
    isLoading.value = true
    application.value = await applicationAPI.fetchOne(appName) as Application
  } catch (err) {
    console.error('Failed to load application:', err)
  } finally {
    isLoading.value = false
  }
}

function goBack() {
  router.push(`/projects/${projectName}`)
}

// ####################################################################################################
// Edit Application
const isEditDialogOpen = ref(false)
const editFormData = ref({
  label: '',
  description: '',
  repo: '',
  path: '',
  env: '',
  args: '',
})
const editDialogErrorMessage = ref('')
const isClickedEditConfirm = ref(false)

function openEditDialog() {
  editFormData.value.label = application.value?.label || ''
  editFormData.value.description = application.value?.description || ''
  editFormData.value.repo = application.value?.repo || ''
  editFormData.value.path = application.value?.path || ''
  editFormData.value.env = application.value?.env || ''
  editFormData.value.args = application.value?.args || ''
  editDialogErrorMessage.value = ''
  isEditDialogOpen.value = true
}

async function handleUpdateApplication() {
  editDialogErrorMessage.value = ''

  try {
    isClickedEditConfirm.value = true
    await applicationAPI.update(appName, {
      label: editFormData.value.label || null,
      description: editFormData.value.description || null,
      repo: editFormData.value.repo || null,
      path: editFormData.value.path || null,
      env: editFormData.value.env || null,
      args: editFormData.value.args || null,
    })
    await loadApplication()
    isEditDialogOpen.value = false
    toast.success('Application updated successfully')
  } catch (err) {
    editDialogErrorMessage.value = err instanceof Error ? err.message : 'Failed to update application'
  } finally {
    isClickedEditConfirm.value = false
  }
}
// ####################################################################################################

// ####################################################################################################
// Delete Application
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
    await applicationAPI.delete(appName)
    isDeleteDialogOpen.value = false
    toast.success(`Application ${appName} deleted successfully`)
    router.push(`/projects/${projectName}`)
  } catch (err) {
    deleteDialogErrorMessage.value = err instanceof Error ? err.message : 'Failed to delete application'
  } finally {
    isClickedDeleteConfirm.value = false
  }
}
// ####################################################################################################

// ####################################################################################################
// Add Container
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
    availableWorkers.value = await applicationAPI.action(`${appName}/get_available_workers`) as Worker[]
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
    await containersAPI.create({ worker: selectedWorker.value }) as Container
    isAddContainerDialogOpen.value = false
    await loadApplication()
    toast.success('Container added successfully')
  } catch (err) {
    addContainerErrorMessage.value = err instanceof Error ? err.message : 'Failed to add container'
  } finally {
    isClickedAddContainerConfirm.value = false
  }
}
// ####################################################################################################
</script>

<template>
  <main class="flex-1 px-4 py-8 relative">
    <div class="mx-auto space-y-6 max-w-7xl">
      <!-- Loading State -->
      <div v-if="isLoading" class="absolute inset-0 z-50 flex items-center justify-center">
        <div class="flex flex-col items-center gap-4">
          <Spinner />
          <p class="text-sm text-muted-foreground">Loading application...</p>
        </div>
      </div>

      <!-- Content -->
      <div v-else-if="application" class="space-y-6">
        <!-- Application Header -->
        <Card>
          <CardHeader class="border-b">
            <CardTitle class="text-2xl">{{ application.label }}</CardTitle>
            <CardDescription>{{ application.name }}</CardDescription>
            <CardAction>
              <ButtonGroup class="space-x-1">
                <!-- Edit Dialog -->
                <Dialog v-model:open="isEditDialogOpen">
                  <DialogTrigger asChild>
                    <Button @click="openEditDialog">Edit</Button>
                  </DialogTrigger>
                  <DialogContent class="sm:max-w-[600px]">
                    <DialogHeader>
                      <DialogTitle>Edit Application</DialogTitle>
                    </DialogHeader>
                    <FieldSet>
                      <FieldGroup>
                        <Field />
                        <Field>
                          <FieldLabel for="label">
                            Name
                          </FieldLabel>
                          <Input id="label" v-model="editFormData.label" placeholder="required" />
                        </Field>
                        <Field>
                          <FieldLabel for="description">
                            Description
                          </FieldLabel>
                          <Textarea id="description" v-model="editFormData.description" class="resize-none"
                            placeholder="optional" />
                        </Field>
                        <Field>
                          <FieldLabel for="repo">
                            Repository URL
                          </FieldLabel>
                          <Input id="repo" v-model="editFormData.repo" placeholder="optional" />
                        </Field>
                        <Field>
                          <FieldLabel for="path">
                            Repository Path
                          </FieldLabel>
                          <Input id="path" v-model="editFormData.path" placeholder="optional" />
                        </Field>
                        <Field>
                          <FieldLabel for="env">
                            Environment Variables
                          </FieldLabel>
                          <Textarea id="env" v-model="editFormData.env" class="resize-none" placeholder="optional" />
                        </Field>
                        <Field>
                          <FieldLabel for="args">
                            Build Arguments
                          </FieldLabel>
                          <Textarea id="args" v-model="editFormData.args" class="resize-none" placeholder="optional" />
                        </Field>
                        <Field>
                          <FieldError v-if="editDialogErrorMessage">{{ editDialogErrorMessage }}</FieldError>
                        </Field>
                      </FieldGroup>
                    </FieldSet>
                    <DialogFooter>
                      <Button type="button" variant="outline" @click="isEditDialogOpen = false"
                        :disabled="isClickedEditConfirm">
                        Cancel
                      </Button>
                      <Button @click="handleUpdateApplication" :disabled="isClickedEditConfirm">
                        <Spinner class="animate-spin" v-if="isClickedEditConfirm" />
                        Save
                      </Button>
                    </DialogFooter>
                  </DialogContent>
                </Dialog>
                <!-- Delete Application Dialog -->
                <Dialog v-model:open="isDeleteDialogOpen">
                  <DialogTrigger asChild>
                    <Button @click="openDeleteApplicationDialog" variant="destructive">Delete</Button>
                  </DialogTrigger>
                  <DialogContent class="sm:max-w-[600px]">
                    <DialogHeader>
                      <DialogTitle>Delete Application</DialogTitle>
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
              </ButtonGroup>
            </CardAction>
          </CardHeader>
          <CardContent class="space-y-4">
            <div>
              <Label>Description</Label>
              <p class="text-sm text-muted-foreground">{{ application.description ? application.description : '-' }}</p>
            </div>
            <div>
              <Label>Repository URL</Label>
              <p class="text-sm text-muted-foreground">{{ application.repo ? application.repo : '-' }}</p>
            </div>
            <div>
              <Label>Repository Path</Label>
              <p class="text-sm text-muted-foreground">{{ application.path ? application.path : '-' }}</p>
            </div>
          </CardContent>
        </Card>

        <!-- Containers -->
        <div class="space-y-4">
          <div class="flex justify-between items-center">
            <h2 class="text-xl font-semibold">Containers</h2>
            <!-- Add Container -->
            <Dialog v-model:open="isAddContainerDialogOpen">
              <DialogTrigger asChild>
                <Button @click="openAddContainerDialog" class="gap-2">
                  <Plus :size="20" />
                  New Container
                </Button>
              </DialogTrigger>
              <DialogContent class="sm:max-w-[400px]">
                <DialogHeader>
                  <DialogTitle>Add Container</DialogTitle>
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
                  <Button type="button" variant="outline" @click="isAddContainerDialogOpen = false"
                    :disabled="isClickedAddContainerConfirm">
                    Cancel
                  </Button>
                  <Button @click="onClickAddContainerConfirm"
                    :disabled="isClickedAddContainerConfirm || !selectedWorker">
                    <Spinner class="animate-spin" v-if="isClickedAddContainerConfirm" />
                    Add
                  </Button>
                </DialogFooter>
              </DialogContent>
            </Dialog>
          </div>

          <!-- Empty Containers State -->
          <div v-if="application.containers.length === 0" class="flex items-center justify-center py-8">
            <Card class="w-full">
              <CardContent class="flex flex-col items-center justify-center py-12">
                <p class="text-muted-foreground text-lg">No containers</p>
              </CardContent>
            </Card>
          </div>

          <!-- Containers Grid -->
          <div v-else class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
            <Card v-for="container in application.containers" :key="container.worker.hostname" class="flex flex-col">
              <CardHeader>
                <CardTitle>
                  <Badge as-child :variant="container.worker.online ? 'default' : 'destructive'">
                    <a href="#">{{ container.worker.hostname }}</a>
                  </Badge>
                </CardTitle>
              </CardHeader>
              <CardContent class="flex-1 space-y-2">
                <p class="text-sm text-muted-foreground">
                  <span class="font-medium">Status:</span> {{ container.status }}
                </p>
              </CardContent>
            </Card>
          </div>
        </div>
      </div>

      <!-- Error/Not Found State -->
      <Card v-else>
        <CardContent class="flex flex-col items-center justify-center py-12">
          <p class="text-muted-foreground text-lg mb-4">Application {{ appName }} not found</p>
          <Button @click="goBack" variant="outline">
            Back to Project
          </Button>
        </CardContent>
      </Card>
    </div>
  </main>
</template>