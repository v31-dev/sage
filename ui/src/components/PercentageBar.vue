<script setup lang="ts">
interface Props {
  value: number
}

const props = withDefaults(defineProps<Props>(), {})

function getStatusColor(percent: number) {
  if (percent < 50) return 'bg-green-500'
  if (percent < 70) return 'bg-yellow-500'
  return 'bg-red-500'
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
  <div class="w-full bg-muted rounded-full h-1.5">
    <div
      :class="['h-1.5 rounded-full transition-all duration-300', getStatusColor(props.value)]"
      :style="{ width: Math.min(props.value, 100) + '%' }"
    ></div>
  </div>
</template>
