<script setup lang="ts">
import { useRouter } from 'vue-router'
import { Trash } from 'lucide-vue-next'
import { toast } from 'vue-sonner'

import ConfirmationButton from '@/components/ConfirmationButton.vue'
import { type Project } from '@/services/api'

interface Props {
  project: Project
  projectAPI: any
}

const props = withDefaults(defineProps<Props>(), {})

const router = useRouter()

async function onConfirmDelete() {
  await props.projectAPI.delete(props.project.name)
  toast.success(`Project ${props.project.name} deleted successfully`)
  router.push('/projects')
}
</script>

<template>
  <ConfirmationButton
    triggerText="Delete"
    title="Delete Project"
    body="Are you sure you want to delete this project? This action cannot be undone."
    :description="props.project.name"
    :icon="Trash"
    destructive
    :onConfirm="onConfirmDelete"
  />
</template>
