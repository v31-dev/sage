<script setup lang="ts">
import CopyButton from '@/components/CopyButton.vue'

defineProps<{ code: string }>()
</script>

<template>
  <div class="relative">
    <pre class="overflow-x-auto rounded-md bg-muted p-3 pr-12 text-xs"><code>{{ code }}</code></pre>
    <CopyButton :text="code" class="absolute right-1 top-1" />
  </div>
</template>
