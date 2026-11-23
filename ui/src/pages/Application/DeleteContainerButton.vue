<script setup lang="ts">
import { computed } from 'vue'
import { Trash } from 'lucide-vue-next'

import ConfirmationButton from '@/components/ConfirmationButton.vue'
import { type Application, type Container, deleteContainer } from '@/services/api'

interface Props {
  application: Application
  container: Container
  loadApplication: () => Promise<void>
}

const props = withDefaults(defineProps<Props>(), {})

// A Delete already running or waiting takes the container away; another would only replace it.
const deleting = computed(
  () =>
    props.container.live === 'delete' ||
    props.container.queued.some(request => request.operation === 'delete')
)

// The names that go with the container.
const body = computed(() => {
  const names = props.container.domains.map(domain => domain.host ?? domain.name)
  return `Are you sure you want to delete this container? This action cannot be undone.${
    names.length ? ` Its domains are deleted with it: ${names.join(', ')}.` : ''
  }`
})

async function onConfirmDeleteContainer() {
  await deleteContainer(props.application.project.name, props.application.name, props.container.id)
  await props.loadApplication()
}
</script>

<template>
  <ConfirmationButton
    triggerText="Delete"
    title="Delete Container"
    :body="body"
    :description="`Worker: ${props.container.worker.hostname}`"
    :icon="Trash"
    icon-only
    destructive
    :disabled="deleting"
    :onConfirm="onConfirmDeleteContainer"
  />
</template>
