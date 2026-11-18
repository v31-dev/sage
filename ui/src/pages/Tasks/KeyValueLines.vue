<script setup lang="ts">
// A task's labels or params as two columns: the name muted, the value beside it (a non-text value as JSON).
const props = defineProps<{ entries: Record<string, unknown> }>()
</script>

<template>
  <dl class="grid grid-cols-[max-content_minmax(0,1fr)] gap-x-2">
    <template v-for="(value, name) in props.entries" :key="name">
      <dt class="text-muted-foreground">{{ name }}</dt>
      <dd>{{ typeof value === 'string' ? value : JSON.stringify(value) }}</dd>
    </template>
  </dl>
</template>
