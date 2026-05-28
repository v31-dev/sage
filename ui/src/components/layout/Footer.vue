<script setup lang="ts">
import { computed } from 'vue'
import { Icon } from '@iconify/vue'
import { Badge } from '@/components/ui/badge'
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '@/components/ui/tooltip'
import { useAppStore } from '@/stores/app'

const appStore = useAppStore()
const releaseUrl = computed(() => {
  const version = appStore.info?.version
  return version ? `https://github.com/v31-dev/sage/releases/tag/v${version}` : null
})
const updateAvailable = computed(() => {
  const info = appStore.info
  return !!(info?.latest_version && info.latest_version !== info.version)
})
const latestReleaseUrl = computed(() => {
  const latest = appStore.info?.latest_version
  return latest ? `https://github.com/v31-dev/sage/releases/tag/v${latest}` : null
})
</script>

<template>
  <footer class="w-full h-10 flex items-center justify-end gap-4 border-t border-border px-4">
    <TooltipProvider v-if="updateAvailable && latestReleaseUrl">
      <Tooltip>
        <TooltipTrigger as-child>
          <Badge as="a" :href="latestReleaseUrl" target="_blank" rel="noreferrer">
            <Icon icon="mdi:arrow-up-bold-circle-outline" />
            v{{ appStore.info?.latest_version }}
          </Badge>
        </TooltipTrigger>
        <TooltipContent> Update available </TooltipContent>
      </Tooltip>
    </TooltipProvider>
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
