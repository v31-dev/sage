<script setup lang="ts">
import { computed } from 'vue'
import { Input } from '@/components/ui/input'
import { useAppStore } from '@/stores/app'
import SettingField from './SettingField.vue'
import SettingsSection from './SettingsSection.vue'
import TraefikResyncSubCard from './TraefikResyncSubCard.vue'

const appStore = useAppStore()
const managerAddress = computed(
  () => `https://${appStore.info?.name}.core.${appStore.info?.domain}`
)
</script>

<template>
  <SettingsSection
    title="Cloudflare"
    description="Domain, tunnel and certificates. Set by environment variables: change them and restart Sage. Records and tunnels of a previous domain or account stay in Cloudflare until you remove them."
    section="cloudflare"
    readonly
  >
    <SettingField id="cloudflare-domain" label="Domain" description="DOMAIN">
      <Input id="cloudflare-domain" :model-value="appStore.info?.domain" readonly />
    </SettingField>
    <SettingField
      id="cloudflare-manager-address"
      label="Manager Address"
      description="The only address the UI is served at"
    >
      <Input id="cloudflare-manager-address" :model-value="managerAddress" readonly />
    </SettingField>
    <SettingField id="cloudflare-tunnel" label="Tunnel" description="Carries public app traffic">
      <Input id="cloudflare-tunnel" :model-value="`${appStore.info?.domain}-sage`" readonly />
    </SettingField>

    <template #after>
      <TraefikResyncSubCard />
    </template>
  </SettingsSection>
</template>
