<script setup lang="ts">
import { computed } from 'vue'
import { ExternalLink } from 'lucide-vue-next'
import { Button } from '@/components/ui/button'
import { useAppStore } from '@/stores/app'
import SettingsSection from './SettingsSection.vue'
import WorkersResyncSubCard from './WorkersResyncSubCard.vue'

const appStore = useAppStore()

const fields = computed(() => [
  {
    key: 'worker_tag',
    label: 'Worker Tag',
    description: 'TS_WORKER_TAG',
    readonly: true,
    value: `tag:${appStore.info?.worker_tag}`,
  },
  {
    key: 'manager',
    label: 'This Manager',
    description: 'TS_NAME and its tailnet IP',
    readonly: true,
    value: `${appStore.info?.name} (${appStore.info?.ip})`,
  },
])
</script>

<template>
  <SettingsSection
    title="Tailscale"
    description="Machines on your tailnet that carry the worker tag become workers. Set by environment variables: change them and restart Sage."
    section="tailscale"
    :fields="fields"
  >
    <template #links>
      <Button
        as="a"
        variant="outline"
        :href="`https://console.tailscale.com/admin/machines/${appStore.info?.ip}`"
        target="_blank"
        rel="noreferrer"
      >
        <ExternalLink />
        This manager in Tailscale
      </Button>
    </template>

    <template #after>
      <WorkersResyncSubCard />
    </template>
  </SettingsSection>
</template>
