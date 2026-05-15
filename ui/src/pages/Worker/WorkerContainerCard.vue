<script setup lang="ts">
import { computed } from 'vue'
import { ArrowRight } from 'lucide-vue-next'
import { RouterLink } from 'vue-router'
import { Button } from '@/components/ui/button'
import { toast } from 'vue-sonner'
import { Card, CardAction, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Label } from '@/components/ui/label'

import ConfirmationButton from '@/components/ConfirmationButton.vue'
import {
  APPLICATION_BUSY_STATUSES,
  type Container,
  type Worker,
  getContainerAPI,
} from '@/services/api'
import TitleStatus from '@/components/TitleStatus.vue'

interface Props {
  worker: Worker
  container: Container
  loadWorker: () => Promise<void>
}

const props = withDefaults(defineProps<Props>(), {})

const isContainerBusy = computed(() => APPLICATION_BUSY_STATUSES.includes(props.container.status))
const showForceDelete = computed(() => !props.worker.online)
const containersAPI = getContainerAPI(
  props.container.application.project.name,
  props.container.application.name
)

const containerStatusClass = computed(() => {
  if (props.container.status === 'active') return 'success'
  if (isContainerBusy.value) return 'warning'
  if (props.container.status === 'error') return 'error'
  return 'default'
})

async function onConfirmForceDelete() {
  await containersAPI.delete(props.worker.hostname, { force: true })

  toast.success(`Force delete initiated for ${props.container.application.label}`)
  await props.loadWorker()
}
</script>

<template>
  <Card class="flex flex-col">
    <CardHeader class="border-b">
      <CardTitle>
        <TitleStatus
          :title="props.container.application.label"
          :status="containerStatusClass"
          :loading="isContainerBusy"
          :statusText="props.container.status"
        />
      </CardTitle>
      <CardAction v-if="showForceDelete">
        <ConfirmationButton
          title="Container"
          mode="delete"
          buttonText="Force Delete"
          :description="`Force delete ${props.container.application.label} from offline worker ${props.worker.hostname}. Remote cleanup on the worker will be skipped.`"
          :onConfirm="onConfirmForceDelete"
        />
      </CardAction>
    </CardHeader>

    <CardContent class="space-y-4">
      <div>
        <Label>Project</Label>
        <Button as-child variant="outline" size="sm" class="mt-2 w-full min-w-0">
          <RouterLink
            :to="`/projects/${props.container.application.project.name}`"
            class="w-full min-w-0"
            :title="`Project: ${props.container.application.project.label}`"
          >
            <span class="block truncate">{{ props.container.application.project.label }}</span
            ><ArrowRight />
          </RouterLink>
        </Button>
      </div>

      <div>
        <Label>Application</Label>
        <Button as-child variant="outline" size="sm" class="mt-2 w-full min-w-0">
          <RouterLink
            :to="`/projects/${props.container.application.project.name}/${props.container.application.name}`"
            class="w-full min-w-0"
            :title="`Application: ${props.container.application.label}`"
          >
            <span class="block truncate">{{ props.container.application.label }}</span
            ><ArrowRight />
          </RouterLink>
        </Button>
      </div>
    </CardContent>
  </Card>
</template>
