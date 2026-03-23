<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { Plus } from 'lucide-vue-next'
import { Card, CardContent, CardHeader, CardTitle, CardFooter, CardAction } from '@/components/ui/card'
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

import { projectAPI, type Project } from '@/services/api'
import CardDescription from '@/components/ui/card/CardDescription.vue'

const router = useRouter()
const projects = ref<Project[]>([])
const isLoading = ref(true)
const isDialogOpen = ref(false)
const isSubmitting = ref(false)

const formData = ref({
  label: '',
  description: ''
})

const errorMessage = ref('')

onMounted(async () => {
  await loadProjects()
})

async function loadProjects() {
  try {
    isLoading.value = true
    projects.value = await projectAPI.fetchAll() as Project[]
  } catch (err) {
    console.error('Failed to load projects:', err)
  } finally {
    isLoading.value = false
  }
}

function openDialog() {
  formData.value = { label: '', description: '' }
  errorMessage.value = ''
  isDialogOpen.value = true
}

async function handleCreateProject() {
  errorMessage.value = ''
  
  if (!formData.value.label.trim()) {
    errorMessage.value = 'Project name is required'
    return
  }

  try {
    isSubmitting.value = true
    await projectAPI.create({ 
      label: formData.value.label, 
      description: formData.value.description || null
    })
    isDialogOpen.value = false
    await loadProjects()
  } catch (err) {
    errorMessage.value = err instanceof Error ? err.message : 'Failed to create project'
  } finally {
    isSubmitting.value = false
  }
}

function goToProject(projectName: string) {
  router.push(`/projects/${projectName}`)
}
</script>

<template>
  <main class="flex-1 px-4 py-6">
    <div class="max-w-7xl mx-auto">
      <!-- Header with New Project Button -->
      <div class="pb-6">
        <Card>
          <CardHeader>
            <CardAction>
              <Dialog v-model:open="isDialogOpen">
                <DialogTrigger asChild>
                  <Button @click="openDialog" class="gap-2">
                    <Plus :size="20" />
                    New Project
                  </Button>
                </DialogTrigger>
                <DialogContent class="sm:max-w-[425px]">
                  <DialogHeader class="pb-6">
                    <DialogTitle>Create New Project</DialogTitle>
                  </DialogHeader>
                  <FieldSet>
                    <FieldGroup>
                      <Field>
                        <FieldLabel for="label">
                          Project Label
                        </FieldLabel>
                        <Input id="label" v-model="formData.label" placeholder="required" />
                      </Field>
                      <Field>
                        <FieldLabel for="description">
                          Description
                        </FieldLabel>
                        <Textarea id="description" v-model="formData.description" class="resize-none" placeholder="optional" />
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
                      @click="handleCreateProject"
                      :disabled="isSubmitting"
                    >
                      {{ isSubmitting ? 'Creating...' : 'Create Project' }}
                    </Button>
                  </DialogFooter>
                </DialogContent>
              </Dialog>
            </CardAction>
          </CardHeader>
        </Card>
      </div>
      
      <!-- Projects Grid -->
      <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
        <Card
          v-for="project in projects"
          :key="project.name"
          class="cursor-pointer hover:shadow-lg transition-shadow flex flex-col min-h-[250px]"
          @click="goToProject(project.name)"
        >
          <CardHeader>
            <CardTitle class="line-clamp-2">{{ project.label }}</CardTitle>
            <CardDescription>{{ project.name }}</CardDescription>
          </CardHeader>
          <CardContent class="flex-1">
            <h3 class="text-sm font-medium text-muted-foreground mb-2">{{ project.description }}</h3>
          </CardContent>
          <CardFooter class="border-t">
            <div class="pt-4 text-xs text-muted-foreground">
              <p>Updated: {{ new Date(project.updated_at).toLocaleString() }}</p>
            </div>
          </CardFooter>
        </Card>
      </div>
    </div>
  </main>
</template>