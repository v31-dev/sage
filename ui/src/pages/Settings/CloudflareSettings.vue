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
import TraefikResyncSubCard from './TraefikResyncSubCard.vue'

const appStore = useAppStore()
const details = computed(() => appStore.info?.settings?.cloudflare.details ?? null)

const domain = computed(() => appStore.info?.domain)
const managerAddress = computed(() => `https://${appStore.info?.name}.core.${domain.value}`)
const tunnel = computed(() => {
  const status = details.value?.tunnel_status
  return (details.value?.tunnel_name ?? '') + (status ? ` (${status})` : '')
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
  {
    key: 'manager_certificate',
    label: 'Manager Certificate',
    description: 'For the manager address; never leaves the manager',
    readonly: true,
    value: expiry(details.value?.manager_certificate_expiry),
  },
  {
    key: 'workers_certificate',
    label: "Workers' Certificate",
    description: 'For the internal and TCP names, copied to every worker',
    readonly: true,
    value: expiry(details.value?.workers_certificate_expiry),
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
              <Field>
                <FieldLabel>Names of the Workers' Certificate</FieldLabel>
                <FieldDescription>
                  Each worker's internal names, and *.int. A new worker or subdomain reissues it.
                </FieldDescription>
                <div data-certificate-names class="flex flex-wrap gap-2">
                  <StateIndicator
                    v-for="name in details.workers_certificate_names"
                    :key="name.name"
                    :state="name.covered ? 'ok' : 'warning'"
                    :label="name.covered ? name.name : `${name.name}: not issued yet`"
                    show-label
                  />
                </div>
                <FieldError v-if="details.workers_certificate_error">
                  The last issuance failed: {{ details.workers_certificate_error }}
                </FieldError>
              </Field>
            </FieldGroup>
          </FieldSet>
        </CardContent>
      </Card>
      <TraefikResyncSubCard />
    </template>
  </SettingsSection>
</template>
