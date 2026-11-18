<script setup lang="ts">
import { computed } from 'vue'
import StateIndicator from '@/components/StateIndicator.vue'
import { TONES } from '@/lib/tone'
import type { Backup, BackupState, IndicatorState } from '@/services/api'

// A backup's state, labelled unless it was taken, and why one was not taken.
const props = defineProps<{ backup: Backup }>()

const STATES: Record<BackupState, { state: IndicatorState; label: string }> = {
  queued: { state: 'idle', label: 'Queued' },
  running: { state: 'busy', label: 'In progress' },
  succeeded: { state: 'ok', label: 'Completed' },
  failed: { state: 'error', label: 'Failed' },
  cancelled: { state: 'idle', label: 'Cancelled' },
  interrupted: { state: 'warning', label: 'Interrupted' },
}

const indicator = computed(() => STATES[props.backup.status])
const reasonClass = computed(() =>
  props.backup.status === 'failed' ? TONES.error.text : TONES.neutral.text
)
</script>

<template>
  <span class="inline-flex flex-col gap-1">
    <StateIndicator v-bind="indicator" :show-label="props.backup.status !== 'succeeded'" />
    <span v-if="props.backup.reason" :class="['text-sm', reasonClass]" data-reason>
      {{ props.backup.reason }}
    </span>
  </span>
</template>
