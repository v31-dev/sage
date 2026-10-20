<script setup lang="ts">
import { computed, type Component } from 'vue'
import { CircleCheck, CircleDashed, LoaderCircle, OctagonX, TriangleAlert } from 'lucide-vue-next'
import { Badge } from '@/components/ui/badge'
import { TONES, type Tone } from '@/lib/tone'
import type { IndicatorState } from '@/services/api'

// A state as an icon (default), an icon with its label in a badge (`showLabel`), or a small
// dot (`dot`; the caller sizes and places it). The label is always the accessible name.
const props = defineProps<{
  state: IndicatorState
  label: string
  showLabel?: boolean
  dot?: boolean
}>()

// Each state has its own icon shape as well as a colour, so colour is never the only cue.
const STATES: Record<IndicatorState, { icon: Component; tone: Tone }> = {
  idle: { icon: CircleDashed, tone: 'neutral' },
  busy: { icon: LoaderCircle, tone: 'info' },
  ok: { icon: CircleCheck, tone: 'success' },
  warning: { icon: TriangleAlert, tone: 'warning' },
  error: { icon: OctagonX, tone: 'error' },
}

const icon = computed(() => STATES[props.state].icon)
const tone = computed(() => TONES[STATES[props.state].tone])
</script>

<template>
  <span
    v-if="dot"
    role="img"
    :aria-label="label"
    :class="['inline-block shrink-0 rounded-full', tone.dot]"
  />
  <Badge v-else-if="showLabel" variant="outline" :class="tone.text">
    <component :is="icon" :class="state === 'busy' && 'animate-spin'" />
    {{ label }}
  </Badge>
  <component
    :is="icon"
    v-else
    role="img"
    :aria-label="label"
    :class="['size-4 shrink-0', tone.text, state === 'busy' && 'animate-spin']"
  />
</template>
