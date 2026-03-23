<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { Plus } from 'lucide-vue-next'
import { Card, CardContent, CardHeader, CardTitle, CardFooter } from '@/components/ui/card'
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

const router = useRouter()
const projects = ref<Project[]>([])
const isLoading = ref(true)
const isDialogOpen = ref(false)
const isSubmitting = ref(false)

const formData = ref({
  name: '',
  description: '',
  env: '',
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
  formData.value = { name: '', description: '', env: '' }
  errorMessage.value = ''
  isDialogOpen.value = true
}

async function handleCreateProject() {
  errorMessage.value = ''
  
  if (!formData.value.name.trim()) {
    errorMessage.value = 'Project name is required'
    return
  }

  try {
    isSubmitting.value = true
    await projectAPI.create({ 
      name: formData.value.name, 
      description: formData.value.description || null,
      env: formData.value.env || null 
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
  <main class="flex-1 px-4 py-8">
    <div class="max-w-7xl mx-auto">
      <!-- Header with New Project Button -->
      <div class="flex justify-between items-center mb-8">
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
                  <FieldLabel for="name">
                    Project Name
                  </FieldLabel>
                  <Input id="name" v-model="formData.name" placeholder="required" />
                </Field>
                <Field>
                  <FieldLabel for="description">
                    Description
                  </FieldLabel>
                  <Textarea id="description" v-model="formData.description" class="resize-none" placeholder="optional" />
                </Field>
                <Field>
                  <FieldLabel for="env">
                    Environment Variables
                  </FieldLabel>
                  <Textarea id="env" v-model="formData.env" class="resize-none" placeholder="optional" />
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
      </div>

      <!-- Loading State -->
      <div v-if="isLoading" class="flex items-center justify-center py-16">
        <div class="animate-spin rounded-full h-12 w-12 border-b-2 border-primary"></div>
      </div>

      <!-- Empty State -->
      <div v-else-if="projects.length === 0" class="flex items-center justify-center py-16">
        <Card class="w-full max-w-md">
          <CardContent class="flex flex-col items-center justify-center py-12">
            <p class="text-muted-foreground text-lg mb-4">No projects yet</p>
          </CardContent>
        </Card>
      </div>

      <!-- Projects Grid -->
      <div v-else class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
        <Card
          v-for="project in projects"
          :key="project.name"
          class="cursor-pointer hover:shadow-lg transition-shadow flex flex-col min-h-[250px]"
          @click="goToProject(project.name)"
        >
          <CardHeader>
            <CardTitle class="line-clamp-2">{{ project.name }}</CardTitle>
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