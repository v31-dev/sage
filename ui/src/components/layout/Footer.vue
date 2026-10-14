<script setup lang="ts">
import { computed } from 'vue'
import { Icon } from '@iconify/vue'
import { Badge } from '@/components/ui/badge'
import { useAppStore } from '@/stores/app'

const appStore = useAppStore()

const updateAvailable = computed(() => {
  const info = appStore.info
  return !!(info?.latest_version && info.latest_version !== info.version)
})
</script>

<template>
  <footer class="w-full h-10 flex items-center justify-end gap-4 border-t border-border px-4">
    <RouterLink to="/settings/upgrade" class="inline-flex items-center">
      <Badge v-if="updateAvailable">
        <Icon icon="mdi:arrow-up-bold-circle-outline" />
        v{{ appStore.info?.latest_version }}
      </Badge>
      <span v-else class="text-xs text-muted-foreground transition-colors hover:text-foreground">
        v{{ appStore.info?.version }}
      </span>
    </RouterLink>
    <a
      href="https://github.com/v31-dev/sage"
      target="_blank"
      class="text-muted-foreground hover:text-foreground transition-colors"
    >
      <Icon icon="mdi:github" class="w-5 h-5" />
    </a>
  </footer>
</template>
