<script setup lang="ts">
import { Trash } from 'lucide-vue-next'
import { toast } from 'vue-sonner'

import ConfirmationButton from '@/components/ConfirmationButton.vue'
import { deleteProject, type Project } from '@/services/api'

interface Props {
  project: Project
  loadProjects: () => Promise<void>
}

const props = withDefaults(defineProps<Props>(), {})

async function onConfirmDelete() {
  await deleteProject(props.project.name)
  toast.success(`Project ${props.project.name} deleted successfully`)
  await props.loadProjects()
}
</script>

<template>
  <ConfirmationButton
    triggerText="Delete"
    title="Delete Project"
    body="Are you sure you want to delete this project? This action cannot be undone."
    :description="props.project.name"
    :icon="Trash"
    icon-only
    destructive
    :disabled="props.project.application_count > 0"
    :onConfirm="onConfirmDelete"
  />
</template>
