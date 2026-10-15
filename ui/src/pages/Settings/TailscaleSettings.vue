<script setup lang="ts">
import { ExternalLink } from 'lucide-vue-next'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { useAppStore } from '@/stores/app'
import SettingField from './SettingField.vue'
import SettingsSection from './SettingsSection.vue'
import WorkersResyncSubCard from './WorkersResyncSubCard.vue'

const appStore = useAppStore()
</script>

<template>
  <SettingsSection
    title="Tailscale"
    description="Machines on your tailnet that carry the worker tag become workers. Set by environment variables: change them and restart Sage."
    section="tailscale"
    readonly
  >
    <SettingField id="tailscale-worker-tag" label="Worker Tag" description="TS_WORKER_TAG">
      <Input id="tailscale-worker-tag" :model-value="`tag:${appStore.info?.worker_tag}`" readonly />
    </SettingField>
    <SettingField
      id="tailscale-manager"
      label="This Manager"
      description="TS_NAME and its tailnet IP"
    >
      <Input
        id="tailscale-manager"
        :model-value="`${appStore.info?.name} (${appStore.info?.ip})`"
        readonly
      />
    </SettingField>

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
