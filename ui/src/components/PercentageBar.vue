<script setup lang="ts">
import { computed } from 'vue'
import { Progress } from '@/components/ui/progress'

interface Props {
  value: number
}

const props = withDefaults(defineProps<Props>(), {})

// The label keeps the raw reading; the bar needs a usable 0-100 track position
const barValue = computed(() =>
  Number.isFinite(props.value) ? Math.min(Math.max(props.value, 0), 100) : 0
)

function getStatusColor(percent: number) {
  if (percent < 50) return '[&>div]:bg-green-500'
  if (percent < 70) return '[&>div]:bg-yellow-500'
  return '[&>div]:bg-red-500'
}

function getStatusTextColor(percent: number) {
  if (percent < 50) return 'text-green-600'
  if (percent < 70) return 'text-yellow-600'
  return 'text-red-600'
}
</script>

<template>
  <div class="flex justify-between items-center mb-1">
    <span class="text-xs font-semibold" :class="getStatusTextColor(props.value)">
      {{ Math.round(props.value * 10) / 10 }}%
    </span>
  </div>
  <Progress class="h-1.5 bg-muted" :class="getStatusColor(props.value)" :model-value="barValue" />
</template>
