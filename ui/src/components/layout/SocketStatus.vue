<script setup lang="ts">
import { computed } from 'vue'
import StateIndicator from '@/components/StateIndicator.vue'
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '@/components/ui/tooltip'
import { connection } from '@/lib/updates'
import { TONES } from '@/lib/tone'
import { formatDateStringAgo } from '@/lib/utils'
import type { Indicator } from '@/services/api'

// Whether the manager's live updates reach this page. The dot pulses each time the manager's heartbeat arrives.
const indicator = computed<Indicator>(() => {
  if (connection.state === 'connected') {
    return connection.transport === 'websocket'
      ? { state: 'ok', label: 'Live updates are on' }
      : { state: 'warning', label: 'Live updates are on, over a slower connection' }
  }
  return connection.state === 'connecting'
    ? { state: 'busy', label: 'Connecting to the manager' }
    : { state: 'error', label: 'Not connected to the manager, trying again' }
})

const tooltip = computed(() =>
  connection.heartbeatAt === null
    ? indicator.value.label
    : `${indicator.value.label}. Last heartbeat ${formatDateStringAgo(new Date(connection.heartbeatAt))}`
)

const pulse = computed(() =>
  connection.state === 'connected' && connection.heartbeatAt !== null
    ? connection.heartbeatAt
    : null
)
const pulseClass = computed(() =>
  indicator.value.state === 'ok' ? TONES.success.dot : TONES.warning.dot
)
</script>

<template>
  <TooltipProvider>
    <Tooltip>
      <TooltipTrigger as-child>
        <span class="relative flex size-2.5 items-center justify-center">
          <span
            v-if="pulse !== null"
            :key="pulse"
            aria-hidden="true"
            :class="[
              'absolute inline-flex size-full rounded-full opacity-75',
              '[animation:ping_1s_cubic-bezier(0,0,0.2,1)_1]',
              pulseClass,
            ]"
          />
          <StateIndicator
            :state="indicator.state"
            :label="indicator.label"
            dot
            class="relative size-2.5"
          />
        </span>
      </TooltipTrigger>
      <TooltipContent side="right">{{ tooltip }}</TooltipContent>
    </Tooltip>
  </TooltipProvider>
</template>
