<script setup lang="ts">
import { computed } from 'vue'
import { ExternalLink } from 'lucide-vue-next'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import {
  Field,
  FieldDescription,
  FieldError,
  FieldGroup,
  FieldLabel,
  FieldSet,
} from '@/components/ui/field'
import StateIndicator from '@/components/StateIndicator.vue'
import { formatDate } from '@/lib/utils'
import { useAppStore } from '@/stores/app'
import SettingsSection from './SettingsSection.vue'

const appStore = useAppStore()
const details = computed(() => appStore.info?.settings?.cloudflare.details ?? null)

const domain = computed(() => appStore.info?.domain)
const managerAddress = computed(() => `https://${appStore.info?.name}.core.${domain.value}`)
const tunnel = computed(() => {
  const status = details.value?.tunnel_status
  return (details.value?.tunnel_name ?? '') + (status ? ` (${status})` : '')
})

function expiry(value: string | null | undefined) {
  return value ? `Expires ${formatDate(value)}` : 'Not issued yet'
}

const certificateLabels: Record<string, string> = {
  manager: 'Manager certificate',
  discovery: 'Discovery certificate',
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

const fields = computed(() => [
  { key: 'domain', label: 'Domain', description: 'DOMAIN', readonly: true, value: domain.value },
  {
    key: 'manager_address',
    label: 'Manager Address',
    description: 'The only address the UI is served at',
    readonly: true,
    value: managerAddress.value,
  },
  {
    key: 'tunnel',
    label: 'Platform Tunnel',
    description: "Run on every worker; each worker's own tunnel carries its public names",
    readonly: true,
    value: tunnel.value,
  },
])
</script>

<template>
  <SettingsSection
    title="Cloudflare"
    description="Domain, tunnel and certificates. Set by environment variables: change them and restart Sage. Records and tunnels of a previous domain or account stay in Cloudflare until you remove them."
    section="cloudflare"
    :fields="fields"
  >
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
      <Card v-if="details">
        <CardContent>
          <FieldSet>
            <FieldGroup>
              <Field v-for="certificate in details.certificates" :key="certificate.id">
                <FieldLabel>{{
                  certificateLabels[certificate.id] ?? `Worker ${certificate.id.slice(7)}`
                }}</FieldLabel>
                <FieldDescription>{{
                  certificate.names.length ? expiry(certificate.expiry) : 'Pending retirement'
                }}</FieldDescription>
                <div data-certificate-names class="flex flex-wrap gap-2">
                  <StateIndicator
                    v-for="name in certificate.names"
                    :key="name"
                    :state="certificate.valid ? 'ok' : 'warning'"
                    :label="certificate.valid ? name : `${name}: not ready`"
                    show-label
                  />
                </div>
                <FieldError v-if="certificate.error">
                  {{ certificate.error }}
                </FieldError>
              </Field>
            </FieldGroup>
          </FieldSet>
        </CardContent>
      </Card>
    </template>
  </SettingsSection>
</template>
