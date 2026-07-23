<script setup lang="ts">
import { Icon } from '@iconify/vue'
import UpgradeDialog from '@/components/layout/UpgradeDialog.vue'
</script>

<template>
  <footer class="w-full h-10 flex items-center justify-end gap-4 border-t border-border px-4">
    <UpgradeDialog />
    <a
      href="https://github.com/v31-dev/sage"
      target="_blank"
      class="text-muted-foreground hover:text-foreground transition-colors"
    >
      <Icon icon="mdi:github" class="w-5 h-5" />
    </a>
  </footer>
</template>
