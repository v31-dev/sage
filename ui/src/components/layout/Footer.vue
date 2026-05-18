<script setup lang="ts">
import { computed } from 'vue'
import { Icon } from '@iconify/vue'
import { useAppStore } from '@/stores/app'

const appStore = useAppStore()
const releaseUrl = computed(() => {
  const version = appStore.info?.version
  return version ? `https://github.com/v31-dev/sage/releases/tag/v${version}` : null
})
</script>

<template>
  <footer class="w-full h-10 flex items-center justify-end gap-4 border-t border-border px-4">
    <a
      v-if="appStore.info?.version && releaseUrl"
      :href="releaseUrl"
      target="_blank"
      rel="noreferrer"
      class="text-xs text-muted-foreground transition-colors hover:text-foreground"
    >
      v{{ appStore.info.version }}
    </a>
    <span v-else class="text-xs text-muted-foreground">v{{ appStore.info?.version }}</span>
    <a
      href="https://github.com/v31-dev/sage"
      target="_blank"
      class="text-muted-foreground hover:text-foreground transition-colors"
    >
      <Icon icon="mdi:github" class="w-5 h-5" />
    </a>
  </footer>
</template>
