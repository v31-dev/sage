<script setup lang="ts">
import { computed, ref } from 'vue'
import { toast } from 'vue-sonner'
import { X } from 'lucide-vue-next'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Spinner } from '@/components/ui/spinner'

import StateIndicator from '@/components/StateIndicator.vue'
import {
  type Container,
  type IndicatorState,
  type QueuedRequest,
  cancelContainerRequest,
  OPERATION_LABELS,
} from '@/services/api'
import { formatDateStringAgo } from '@/lib/utils'
import { useAppStore } from '@/stores/app'

// A container's state as the manager decides it: its headline, how its configuration compares with its deployment, why
// it is in error, what is known of its runtime, and the requests waiting their turn on it, each with its own Cancel.
const props = defineProps<{
  container: Container
  // Loads the page again once a Cancel is done.
  reload: () => Promise<void>
}>()

const appStore = useAppStore()

const CONFIGS: Partial<Record<Container['view']['config'], string>> = {
  dirty: 'Configuration changed',
  not_deployed: 'Not deployed',
}

// What each reason means; `detail` when the manager's detail reads as part of it.
const REASONS: Record<string, { text: string; detail?: boolean }> = {
  restored: { text: 'Its deployment is from before the platform was restored' },
  authority: { text: 'Another Sage manages it' },
  invalid_record: { text: "Its record on the worker can't be read" },
  backup_interrupted: {
    text: 'An automatic backup was interrupted; it is being put back as it was',
  },
  interrupted: { text: 'An operation on it was interrupted' },
  failed: { text: 'The last operation failed', detail: true },
  unreachable: { text: "Its worker can't be reached" },
  unexpected_running: { text: 'It is running though it was stopped' },
  unexpected_exit: { text: 'It stopped unexpectedly' },
  unexpected_deployment: { text: 'It is running a deployment it was not deployed with' },
}

const HEALTH: Record<string, IndicatorState> = {
  healthy: 'ok',
  unhealthy: 'error',
  starting: 'busy',
}

const reason = computed(() => {
  const { reason, detail } = props.container.view
  if (!reason) return ''
  const meaning = REASONS[reason]
  if (!meaning) return reason
  return meaning.detail && detail ? `${meaning.text}: ${detail}` : `${meaning.text}.`
})

// What is known of the runtime: Docker's state as last read, said to be the last one seen when it is not current.
const runtime = computed(() => {
  const { knowledge, reading } = props.container.runtime
  if (!reading) {
    return knowledge === 'unreachable'
      ? "Its worker can't be reached; not seen yet"
      : 'Not seen on its worker yet'
  }
  const exit =
    reading.state !== 'running' && reading.exit_code !== null
      ? ` (exit code ${reading.exit_code}${reading.oom_killed ? ', out of memory' : ''})`
      : ''
  const state = reading.state === 'absent' ? 'not on its worker' : `${reading.state}${exit}`
  if (knowledge === 'known') return `Docker: ${state}`
  return knowledge === 'unreachable'
    ? `Worker unreachable; last seen ${state}`
    : `Last seen ${state}`
})

const health = computed(() => {
  const value = props.container.runtime.reading?.health
  return value && value in HEALTH ? { state: HEALTH[value]!, label: `health ${value}` } : undefined
})

const deployedAt = computed(() => props.container.target.record?.applied?.committed_at ?? null)

const cancelling = ref('')

async function onClickCancel(request: QueuedRequest) {
  cancelling.value = request.task_id
  try {
    await cancelContainerRequest(
      props.container.application.project.name,
      props.container.application.name,
      props.container.id,
      request.operation
    )
    await props.reload()
  } catch (err) {
    toast.error(err instanceof Error ? err.message : 'Failed to cancel')
  } finally {
    cancelling.value = ''
  }
}
</script>

<template>
  <div class="space-y-2" data-container-status>
    <div class="flex flex-wrap items-center gap-2">
      <StateIndicator
        v-bind="appStore.containerIndicator(props.container.view.headline)"
        show-label
      />
      <Badge v-if="CONFIGS[props.container.view.config]" variant="outline" data-config>
        {{ CONFIGS[props.container.view.config] }}
      </Badge>
      <StateIndicator v-if="health" v-bind="health" show-label />
    </div>
    <p v-if="reason" class="text-sm text-destructive" data-reason>{{ reason }}</p>
    <p class="text-sm text-muted-foreground" data-runtime>{{ runtime }}</p>
    <p v-if="deployedAt" class="text-sm text-muted-foreground">
      Deployed {{ formatDateStringAgo(deployedAt) }}
    </p>
    <div
      v-for="request in props.container.queued"
      :key="request.task_id"
      class="flex items-center justify-between gap-2 text-sm"
      data-queued
    >
      <span class="text-muted-foreground">{{ OPERATION_LABELS[request.operation] }} queued</span>
      <Button
        v-if="request.cancellable"
        size="sm"
        variant="outline"
        :disabled="cancelling !== ''"
        @click="onClickCancel(request)"
      >
        <Spinner v-if="cancelling === request.task_id" class="animate-spin" />
        <X v-else />Cancel
      </Button>
    </div>
  </div>
</template>
