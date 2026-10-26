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
    description="The manager's node on your tailnet. Workers are added by hand on the Workers page. Set by environment variables: change them and restart Sage."
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
