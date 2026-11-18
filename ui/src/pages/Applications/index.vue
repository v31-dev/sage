<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { Plus } from 'lucide-vue-next'
import { Card, CardHeader, CardAction } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from '@/components/ui/dialog'
import { Field, FieldError, FieldGroup, FieldLabel, FieldSet } from '@/components/ui/field'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { Spinner } from '@/components/ui/spinner'

import { createProject, fetchProjects, type ProjectPage } from '@/services/api'
import ProjectCard from './ProjectCard.vue'

const projects = ref<ProjectPage[]>([])
const isLoading = ref(true)
const isDialogOpen = ref(false)
const isSubmitting = ref(false)

const formData = ref({
  label: '',
  description: '',
})

const errorMessage = ref('')

onMounted(async () => {
  await loadProjects()
})

let lastLoad = 0

async function loadProjects() {
  const load = ++lastLoad
  try {
    const next = await fetchProjects()
    if (load === lastLoad) projects.value = next
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
    await createProject({
      label: formData.value.label,
      description: formData.value.description || null,
    })
    isDialogOpen.value = false
    await loadProjects()
  } catch (err) {
    errorMessage.value = err instanceof Error ? err.message : 'Failed to create project'
  } finally {
    isSubmitting.value = false
  }
}
</script>

<template>
  <main class="flex-1 px-4 py-6 relative">
    <div class="max-w-7xl mx-auto">
      <!-- Loading State -->
      <div v-if="isLoading" class="absolute inset-0 z-50 flex items-center justify-center">
        <div class="flex flex-col items-center gap-4">
          <Spinner />
          <p class="text-sm text-muted-foreground">Loading applications...</p>
        </div>
      </div>

      <!-- Content -->
      <div v-else class="space-y-6">
        <Card>
          <CardHeader>
            <CardAction>
              <Dialog v-model:open="isDialogOpen">
                <DialogTrigger asChild>
                  <Button size="sm" @click="openDialog" class="gap-2">
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
                        <FieldLabel for="label"> Project Label </FieldLabel>
                        <Input id="label" v-model="formData.label" />
                      </Field>
                      <Field>
                        <FieldLabel for="description"> Description (optional) </FieldLabel>
                        <Textarea
                          id="description"
                          v-model="formData.description"
                          class="resize-none"
                        />
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
                      @click="isDialogOpen = false"
                      :disabled="isSubmitting"
                    >
                      Cancel
                    </Button>
                    <Button size="sm" @click="handleCreateProject" :disabled="isSubmitting">
                      {{ isSubmitting ? 'Creating...' : 'Create Project' }}
                    </Button>
                  </DialogFooter>
                </DialogContent>
              </Dialog>
            </CardAction>
          </CardHeader>
        </Card>

        <ProjectCard
          v-for="project in projects"
          :key="project.name"
          :project="project"
          :loadProjects="loadProjects"
        />
      </div>
    </div>
  </main>
</template>
