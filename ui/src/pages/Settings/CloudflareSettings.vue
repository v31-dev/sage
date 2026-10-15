<script setup lang="ts">
import { computed } from 'vue'
import { ExternalLink } from 'lucide-vue-next'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { formatDate } from '@/lib/utils'
import { useAppStore } from '@/stores/app'
import { useSettingsStatusStore } from '@/stores/settingsStatus'
import SettingField from './SettingField.vue'
import SettingsSection from './SettingsSection.vue'
import TraefikResyncSubCard from './TraefikResyncSubCard.vue'

const appStore = useAppStore()
const statusStore = useSettingsStatusStore()
const details = computed(() => statusStore.status?.cloudflare.details ?? null)

const domain = computed(() => appStore.info?.domain)
const managerAddress = computed(() => `https://${appStore.info?.name}.core.${domain.value}`)
const tunnel = computed(() => {
  const status = details.value?.tunnel_status
  return `${domain.value}-sage` + (status ? ` (${status})` : '')
})

function expiry(value: string | null | undefined) {
  return value ? `Valid until ${formatDate(value)}` : 'Not issued yet'
}

const tunnelUrl = computed(() =>
  details.value?.account_id && details.value?.tunnel_id
    ? `https://dash.cloudflare.com/${details.value.account_id}/tunnels/${details.value.tunnel_id}/overview`
    : null
)
const dnsUrl = computed(() =>
  details.value?.account_id
    ? `https://dash.cloudflare.com/${details.value.account_id}/${domain.value}/dns/records`
    : null
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
      <Input id="cloudflare-domain" :model-value="domain" readonly />
    </SettingField>
    <SettingField
      id="cloudflare-manager-address"
      label="Manager Address"
      description="The only address the UI is served at"
    >
      <Input id="cloudflare-manager-address" :model-value="managerAddress" readonly />
    </SettingField>
    <SettingField id="cloudflare-tunnel" label="Tunnel" description="Carries public app traffic">
      <Input id="cloudflare-tunnel" :model-value="tunnel" readonly />
    </SettingField>
    <SettingField
      id="cloudflare-manager-certificate"
      label="Manager Certificate"
      description="For the manager address; never leaves the manager"
    >
      <Input
        id="cloudflare-manager-certificate"
        :model-value="expiry(details?.manager_certificate_expiry)"
        readonly
      />
    </SettingField>
    <SettingField
      id="cloudflare-workers-certificate"
      label="Workers' Certificate"
      description="For internal app domains (*.int), copied to every worker"
    >
      <Input
        id="cloudflare-workers-certificate"
        :model-value="expiry(details?.workers_certificate_expiry)"
        readonly
      />
    </SettingField>

    <template #links>
      <Button
        as="a"
        variant="outline"
        :href="tunnelUrl ?? undefined"
        :disabled="!tunnelUrl"
        target="_blank"
        rel="noreferrer"
      >
        <ExternalLink />
        Tunnel in Cloudflare
      </Button>
      <Button
        as="a"
        variant="outline"
        :href="dnsUrl ?? undefined"
        :disabled="!dnsUrl"
        target="_blank"
        rel="noreferrer"
      >
        <ExternalLink />
        DNS records in Cloudflare
      </Button>
    </template>

    <template #after>
      <TraefikResyncSubCard />
    </template>
  </SettingsSection>
</template>
