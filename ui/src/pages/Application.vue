<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
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
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { application as applicationService, type Application } from '@/services/api'
import CardFooter from '@/components/ui/card/CardFooter.vue'

const route = useRoute()
const router = useRouter()
const projectName = route.params.projectId as string
const appName = route.params.appId as string

const application = ref<Application | null>(null)
const isLoading = ref(true)
const isDialogOpen = ref(false)
const isSubmitting = ref(false)
const errorMessage = ref('')
const successMessage = ref('')

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
    // Fetch using composite key: project,name
    const compositeKey = `${projectName},${appName}`
    application.value = await applicationService.fetchOne(compositeKey) as Application
  } catch (err) {
    console.error('Failed to load application:', err)
  } finally {
    isLoading.value = false
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

async function handleUpdateApplication() {
  errorMessage.value = ''
  successMessage.value = ''

  try {
    isSubmitting.value = true
    // Update using composite key: project,name
    const compositeKey = `${projectName},${appName}`
    await applicationService.update(compositeKey, {
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
