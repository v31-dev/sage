<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { 
  Card, 
  CardAction, 
  CardContent, 
  CardHeader, 
  CardTitle, 
  CardFooter 
} from '@/components/ui/card'
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
import { getApplicationAPI, getContainerAPI, type Application, type Worker, type Container } from '@/services/api'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableRow,

} from '@/components/ui/table'

const route = useRoute()
const router = useRouter()
const projectName = route.params.projectId as string
const appName = route.params.appId as string

const applicationAPI = getApplicationAPI(projectName)
const containersAPI = getContainerAPI(projectName, appName)
const application = ref<Application | null>(null)
const isLoading = ref(true)
const isDialogOpen = ref(false)
const isContainerDialogOpen = ref(false)
const isSubmitting = ref(false)
const errorMessage = ref('')
const successMessage = ref('')
const availableWorkers = ref<Worker[]>([])
const selectedWorker = ref('')

const editFormData = ref({
  description: '',
  repo: '',
  env: '',
  args: '',
})

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

async function loadAvailableWorkers() {
  try {
    availableWorkers.value = await applicationAPI.action(`${appName}/get_available_workers`) as Worker[]
    selectedWorker.value = availableWorkers.value[0]?.hostname || ''
  } catch (err) {
    console.error('Failed to load available workers:', err)
    errorMessage.value = 'Failed to load available workers'
  }
}

function openEditDialog() {
  editFormData.value.description = application.value?.description || ''
  editFormData.value.repo = application.value?.repo || ''
  editFormData.value.env = application.value?.env || ''
  editFormData.value.args = application.value?.args || ''
  errorMessage.value = ''
  successMessage.value = ''
  isDialogOpen.value = true
}

function openAddContainerDialog() {
  errorMessage.value = ''
  successMessage.value = ''
  selectedWorker.value = ''
  loadAvailableWorkers()
  isContainerDialogOpen.value = true
}

async function handleAddContainer() {
  errorMessage.value = ''
  successMessage.value = ''

  if (!selectedWorker.value) {
    errorMessage.value = 'Please select a worker'
    return
  }

  try {
    isSubmitting.value = true
    await containersAPI.create({ worker: selectedWorker.value }) as Container
    await loadApplication()
    isContainerDialogOpen.value = false
    successMessage.value = 'Container added successfully'
    setTimeout(() => {
      successMessage.value = ''
    }, 3000)
  } catch (err) {
    errorMessage.value = err instanceof Error ? err.message : 'Failed to add container'
  } finally {
    isSubmitting.value = false
  }
}

async function deployContainer(worker: string) {
  try {
    // Implementation for deploying a container
    console.log('Deploying container on worker:', worker)
    successMessage.value = 'Container deployed successfully'
    setTimeout(() => {
      successMessage.value = ''
    }, 3000)
  } catch (err) {
    errorMessage.value = 'Failed to deploy container'
  }
}

async function stopContainer(worker: string) {
  try {
    // Implementation for stopping a container
    console.log('Stopping container on worker:', worker)
    successMessage.value = 'Container stopped successfully'
    setTimeout(() => {
      successMessage.value = ''
    }, 3000)
  } catch (err) {
    errorMessage.value = 'Failed to stop container'
  }
}

async function backupContainer(worker: string) {
  try {
    // Implementation for backing up a container
    console.log('Backing up container on worker:', worker)
    successMessage.value = 'Container backup started'
    setTimeout(() => {
      successMessage.value = ''
    }, 3000)
  } catch (err) {
    errorMessage.value = 'Failed to backup container'
  }
}

async function migrateContainer(worker: string) {
  try {
    // Implementation for migrating a container
    console.log('Migrating container from worker:', worker)
    successMessage.value = 'Container migration started'
    setTimeout(() => {
      successMessage.value = ''
    }, 3000)
  } catch (err) {
    errorMessage.value = 'Failed to migrate container'
  }
}

async function deleteContainer(worker: string) {
  if (!confirm(`Are you sure you want to delete the container on ${worker}?`)) {
    return
  }
  
  try {
    // Implementation for deleting a container
    console.log('Deleting container on worker:', worker)
    await loadApplication()
    successMessage.value = 'Container deleted successfully'
    setTimeout(() => {
      successMessage.value = ''
    }, 3000)
  } catch (err) {
    errorMessage.value = 'Failed to delete container'
  }
}

async function handleUpdateApplication() {
  errorMessage.value = ''
  successMessage.value = ''

  try {
    isSubmitting.value = true
    await getApplicationAPI(projectName).update(appName, {
      description: editFormData.value.description || null,
      repo: editFormData.value.repo || null,
      env: editFormData.value.env || null,
      args: editFormData.value.args || null,
    })
    await loadApplication()
    isDialogOpen.value = false
    successMessage.value = 'Application updated successfully'
    setTimeout(() => {
      successMessage.value = ''
    }, 3000)
  } catch (err) {
    errorMessage.value = err instanceof Error ? err.message : 'Failed to update application'
  } finally {
    isSubmitting.value = false
  }
}

function goBackToProject() {
  router.push(`/projects/${projectName}`)
}
</script>

<template>
  <main class="flex-1 px-4 py-8">
    <div class="mx-auto space-y-6 max-w-7xl">
      <!-- Loading State -->
      <div v-if="isLoading" class="flex items-center justify-center py-16">
        <div class="animate-spin rounded-full h-12 w-12 border-b-2 border-primary"></div>
      </div>

      <!-- Content -->
      <div v-else-if="application" class="space-y-6">
        <!-- Success Message -->
        <div v-if="successMessage" class="p-4 bg-green-50 text-green-700 rounded-md">
          {{ successMessage }}
        </div>

        <!-- Application Card -->
        <Card>
          <CardHeader>
            <div class="flex justify-between items-start">
              <div class="flex-1">
                <CardTitle class="text-2xl mb-2">{{ application.name }}</CardTitle>
                <p class="text-sm text-muted-foreground">{{ application.description }}</p>
              </div>
              <Dialog v-model:open="isDialogOpen">
                <DialogTrigger asChild>
                  <Button @click="openEditDialog">Edit</Button>
                </DialogTrigger>
                <DialogContent class="sm:max-w-[600px]">
                  <DialogHeader>
                    <DialogTitle>Edit Application</DialogTitle>
                  </DialogHeader>
                  <FieldSet>
                    <FieldGroup>
                      <Field>
                        <FieldLabel for="app-description">
                          Description
                        </FieldLabel>
                        <Textarea id="app-description" v-model="editFormData.description" class="resize-none" placeholder="optional" />
                      </Field>
                      <Field>
                        <FieldLabel for="app-repo">
                          Repository URL
                        </FieldLabel>
                        <Input id="app-repo" v-model="editFormData.repo" placeholder="optional" />
                      </Field>
                      <Field>
                        <FieldLabel for="app-env">
                          Environment Variables
                        </FieldLabel>
                        <Textarea id="app-env" v-model="editFormData.env" class="resize-none" placeholder="optional" />
                      </Field>
                      <Field>
                        <FieldLabel for="app-args">
                          Arguments
                        </FieldLabel>
                        <Textarea id="app-args" v-model="editFormData.args" class="resize-none" placeholder="optional" />
                      </Field>
                      <Field>
                        <FieldError v-if="errorMessage">{{errorMessage}}</FieldError>
                      </Field>
                    </FieldGroup>
                  </FieldSet>
                  <DialogFooter>
                    <Button
                      type="button"
                      variant="outline"
                      @click="isDialogOpen = false"
                      :disabled="isSubmitting"
                    >
                      Cancel
                    </Button>
                    <Button
                      @click="handleUpdateApplication"
                      :disabled="isSubmitting"
                    >
                      {{ isSubmitting ? 'Saving...' : 'Save Changes' }}
                    </Button>
                  </DialogFooter>
                </DialogContent>
              </Dialog>
            </div>
          </CardHeader>
          <CardContent class="space-y-4">
            <div v-if="application.repo" class="pb-4 border-t pt-4">
              <h3 class="text-sm font-medium text-muted-foreground mb-1">Repository</h3>
              <p class="text-sm break-all">{{ application.repo }}</p>
            </div>
          </CardContent>
          <CardFooter class="border-t">
            <div class="pt-4 space-y-2 text-xs text-muted-foreground">
              <p>Updated: {{ new Date(application.updated_at).toLocaleString() }}</p>
            </div>
          </CardFooter>
        </Card>

        <!-- Containers-->
         <Card>
          <CardHeader>
            <CardTitle class="text-lg">Containers</CardTitle>
            <CardAction>
              <Dialog v-model:open="isContainerDialogOpen">
                <DialogTrigger asChild>
                  <Button @click="openAddContainerDialog">
                    Add Container
                  </Button>
                </DialogTrigger>
                <DialogContent class="sm:max-w-[400px]">
                  <DialogHeader>
                    <DialogTitle>Add Container</DialogTitle>
                  </DialogHeader>
                  <FieldSet>
                    <FieldGroup>
                      <Field>
                        <FieldLabel for="worker-select">
                          Select Worker
                        </FieldLabel>
                        <Select v-model="selectedWorker">
                          <SelectTrigger id="worker-select">
                            <SelectValue placeholder="Choose a worker..." />
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
                      <Field v-if="errorMessage">
                        <FieldError>{{ errorMessage }}</FieldError>
                      </Field>
                    </FieldGroup>
                  </FieldSet>
                  <DialogFooter>
                    <Button
                      type="button"
                      variant="outline"
                      @click="isContainerDialogOpen = false"
                      :disabled="isSubmitting"
                    >
                      Cancel
                    </Button>
                    <Button
                      @click="handleAddContainer"
                      :disabled="isSubmitting || !selectedWorker"
                    >
                      {{ isSubmitting ? 'Adding...' : 'Add Container' }}
                    </Button>
                  </DialogFooter>
                </DialogContent>
              </Dialog>
            </CardAction>
          </CardHeader>
          <CardContent>
            <Table>
              <TableHead>
                <TableRow>
                  <TableHead>Worker</TableHead>
                  <TableHead>Status</TableHead>
                  <TableHead>Actions</TableHead>
                </TableRow>
              </TableHead>
              <TableBody>
                <TableRow v-for="container in application.containers" :key="container.worker.hostname">
                  <TableCell class="font-medium">{{ container.worker.hostname }}</TableCell>
                  <TableCell>{{ container.status }}</TableCell>
                  <TableCell class="space-x-2">
                    <Button 
                      size="sm" 
                      variant="outline"
                      @click="deployContainer(container.worker.hostname)"
                    >
                      Deploy
                    </Button>
                    <Button 
                      size="sm" 
                      variant="outline"
                      @click="stopContainer(container.worker.hostname)"
                    >
                      Stop
                    </Button>
                    <Button 
                      size="sm" 
                      variant="outline"
                      @click="backupContainer(container.worker.hostname)"
                    >
                      Backup
                    </Button>
                    <Button 
                      size="sm" 
                      variant="outline"
                      @click="migrateContainer(container.worker.hostname)"
                    >
                      Migrate
                    </Button>
                    <Button 
                      size="sm" 
                      variant="destructive"
                      @click="deleteContainer(container.worker.hostname)"
                    >
                      Delete
                    </Button>
                  </TableCell>
                </TableRow>
              </TableBody>
            </Table>  
          </CardContent>
         </Card>
      </div>

      <!-- Error/Not Found State -->
      <Card v-else>
        <CardContent class="flex flex-col items-center justify-center py-12">
          <p class="text-muted-foreground text-lg mb-4">Application not found</p>
          <Button @click="goBackToProject" variant="outline">
            Back to Project
          </Button>
        </CardContent>
      </Card>
    </div>
  </main>
</template>
