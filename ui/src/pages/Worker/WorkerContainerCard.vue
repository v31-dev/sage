<script setup lang="ts">
import { computed } from 'vue'
import { ArrowRight, Trash } from 'lucide-vue-next'
import { RouterLink } from 'vue-router'
import { Button } from '@/components/ui/button'
import { toast } from 'vue-sonner'
import { Card, CardAction, CardHeader, CardTitle } from '@/components/ui/card'

import ConfirmationButton from '@/components/ConfirmationButton.vue'
import { type Container, type Worker, getContainerAPI } from '@/services/api'
import SectionTitle from '@/components/SectionTitle.vue'
import StateIndicator from '@/components/StateIndicator.vue'
import { useAppStore } from '@/stores/app'
import CardFooter from '@/components/ui/card/CardFooter.vue'

interface Props {
  worker: Worker
  container: Container
  loadWorker: () => Promise<void>
}

const appStore = useAppStore()

const props = withDefaults(defineProps<Props>(), {})

const showForceDelete = computed(() => !props.worker.online)
const containersAPI = getContainerAPI(
  props.container.application.project.name,
  props.container.application.name
)

async function onConfirmForceDelete() {
  await containersAPI.delete(props.worker.hostname, { force: true })

  toast.success(`Force delete initiated for ${props.container.application.label}`)
  await props.loadWorker()
}
</script>

<template>
  <Card>
    <CardHeader class="flex items-center justify-between">
      <CardTitle>
        <SectionTitle
          :title="
            props.container.application.project.name + '-' + props.container.application.label
          "
        >
          <StateIndicator
            v-bind="appStore.applicationIndicator(props.container.status)"
            show-label
          />
        </SectionTitle>
      </CardTitle>
      <CardAction v-if="showForceDelete">
        <ConfirmationButton
          triggerText="Force Delete"
          title="Delete Container"
          body="Are you sure you want to delete this container? This action cannot be undone."
          :description="`Force delete ${props.container.application.label} from offline worker ${props.worker.hostname}. Remote cleanup on the worker will be skipped.`"
          :icon="Trash"
          destructive
          :onConfirm="onConfirmForceDelete"
        />
      </CardAction>
    </CardHeader>

    <CardFooter class="border-t flex-col space-y-4">
      <Button as-child variant="outline" size="sm" class="w-full min-w-0">
        <RouterLink
          :to="`/projects/${props.container.application.project.name}`"
          class="w-full min-w-0"
          :title="`Project: ${props.container.application.project.label}`"
        >
          <span class="block truncate"
            >Project: {{ props.container.application.project.label }}</span
          ><ArrowRight />
        </RouterLink>
      </Button>

      <Button as-child variant="outline" size="sm" class="w-full min-w-0">
        <RouterLink
          :to="`/projects/${props.container.application.project.name}/${props.container.application.name}`"
          class="w-full min-w-0"
          :title="`Application: ${props.container.application.label}`"
        >
          <span class="block truncate">Application: {{ props.container.application.label }}</span
          ><ArrowRight />
        </RouterLink>
      </Button>
    </CardFooter>
  </Card>
</template>
