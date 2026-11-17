<script setup lang="ts">
// A task's labels or params, one line each: the name muted, then the value (a non-text value as JSON).
const props = defineProps<{ entries: Record<string, unknown> }>()
</script>

<template>
  <div v-for="(value, name) in props.entries" :key="name">
    <span class="text-muted-foreground">{{ name }}:</span>
    {{ typeof value === 'string' ? value : JSON.stringify(value) }}
  </div>
</template>
