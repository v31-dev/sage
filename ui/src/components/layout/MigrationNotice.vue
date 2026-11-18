<script setup lang="ts">
import { watch } from 'vue'
import { useRouter } from 'vue-router'
import { useAppStore } from '@/stores/app'

// After a migration that left steps to do by hand, the Upgrade page opens on its run, once per run in this browser.
const SHOWN = 'sage.migration.shown'

const appStore = useAppStore()
const router = useRouter()

watch(
  () => appStore.info?.migration,
  migration => {
    if (!migration || migration.actions === 0) return
    try {
      if (localStorage.getItem(SHOWN) === migration.run) return
      localStorage.setItem(SHOWN, migration.run)
    } catch {
      // Without storage it is shown at every load, which still shows it.
    }
    router.push({ path: '/upgrade', query: { migration: migration.run } })
  },
  { immediate: true }
)
</script>

<template><span hidden /></template>
