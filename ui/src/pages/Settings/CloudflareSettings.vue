<script setup lang="ts">
import { TriangleAlert } from 'lucide-vue-next'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { Input } from '@/components/ui/input'
import CustomInput from '@/components/CustomInput.vue'
import SettingField from './SettingField.vue'
import SettingsSection from './SettingsSection.vue'
import TraefikResyncSubCard from './TraefikResyncSubCard.vue'
import { useSettingForm } from './useSettingForm'

const form = useSettingForm(
  'cloudflare',
  ['domain', 'admin_email', 'api_token', 'account_id'],
  'Cloudflare'
)
const { fields, isLoading, saving, testing, error, dirty, leaveOpen } = form
</script>

<template>
  <SettingsSection
    title="Cloudflare"
    description="Domain, tunnels and the wildcard certificate for apps. Optional: without it, apps are reached over the tailnet only. All four values are checked together."
    section="cloudflare"
    :loading="isLoading"
    :saving="saving"
    :testing="testing"
    :dirty="dirty"
    :error="error"
    :leave-open="leaveOpen"
    @save="form.save"
    @test="form.test"
    @reset="form.reset"
    @leave="form.resolveLeave"
  >
    <Alert>
      <TriangleAlert />
      <AlertDescription>
        Changing these updates certificates, DNS records and the Cloudflare tunnel. Existing
        connections can be disrupted until DNS updates propagate. Records for an old domain have to
        be removed in Cloudflare by hand.
      </AlertDescription>
    </Alert>
    <SettingField
      id="cloudflare-domain"
      label="Base Domain"
      description="Root domain for application and manager addresses."
    >
      <Input
        id="cloudflare-domain"
        :model-value="fields.domain"
        @update:model-value="value => form.updateField('domain', value)"
        placeholder="example.com"
      />
    </SettingField>
    <SettingField
      id="cloudflare-admin-email"
      label="Admin Email"
      description="Contact email for the certificate's ACME registration."
    >
      <Input
        id="cloudflare-admin-email"
        :model-value="fields.admin_email"
        @update:model-value="value => form.updateField('admin_email', value)"
        type="email"
        placeholder="admin@example.com"
      />
    </SettingField>
    <SettingField
      id="cloudflare-api-token"
      label="API Token"
      description="Used to check the zone, manage the tunnel and write DNS records."
    >
      <CustomInput
        id="cloudflare-api-token"
        secret
        :model-value="fields.api_token"
        @update:model-value="value => form.updateField('api_token', value)"
        placeholder="Cloudflare API token"
      />
    </SettingField>
    <SettingField
      id="cloudflare-account-id"
      label="Account ID"
      description="The account the tunnel is created in."
    >
      <Input
        id="cloudflare-account-id"
        :model-value="fields.account_id"
        @update:model-value="value => form.updateField('account_id', value)"
        placeholder="Cloudflare account id"
      />
    </SettingField>

    <template #after>
      <TraefikResyncSubCard />
    </template>
  </SettingsSection>
</template>
