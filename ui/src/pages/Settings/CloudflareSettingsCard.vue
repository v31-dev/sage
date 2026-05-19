<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { Button } from '@/components/ui/button'
import {
  Card,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from '@/components/ui/card'
import {
  Field,
  FieldDescription,
  FieldError,
  FieldGroup,
  FieldLabel,
  FieldSet,
} from '@/components/ui/field'
import { Input } from '@/components/ui/input'
import { Spinner } from '@/components/ui/spinner'
import { coerceSettingFields, loadSetting, saveSetting, type SettingFields } from './common'
import SecretInput from '@/components/SecretInput.vue'

const CLOUDFLARE_FIELD_KEYS = ['domain', 'admin_email', 'api_token', 'account_id'] as const

const isLoading = ref(true)
const saving = ref(false)
const settings = ref<SettingFields>(coerceSettingFields(CLOUDFLARE_FIELD_KEYS))
const error = ref<string>('')

const load = () =>
  loadSetting({
    settingKey: 'cloudflare',
    fieldKeys: CLOUDFLARE_FIELD_KEYS,
    fields: settings,
    isLoading,
    error,
    fallbackMessage: 'Failed to load Cloudflare settings',
  })

function updateField(fieldKey: string, value: string | number) {
  settings.value[fieldKey] = String(value)
  error.value = ''
}

const onClickSave = () =>
  saveSetting({
    settingKey: 'cloudflare',
    fieldKeys: CLOUDFLARE_FIELD_KEYS,
    fields: settings,
    saving,
    error,
    fallbackMessage: 'Failed to save Cloudflare settings',
    successMessage: 'Cloudflare settings saved successfully',
  })

onMounted(() => {
  load()
})
</script>

<template>
  <Card class="overflow-hidden">
    <CardHeader>
      <CardTitle>Cloudflare</CardTitle>
      <CardDescription>
        Domain, admin email, Cloudflare API token, and account ID are validated together. Domain
        changes update routing immediately, but old-domain cleanup is manual.
      </CardDescription>
    </CardHeader>
    <CardContent class="space-y-6">
      <div
        v-if="isLoading"
        class="flex items-center justify-center py-8 text-sm text-muted-foreground"
      >
        <div class="flex items-center gap-3">
          <Spinner class="animate-spin" />
          Loading settings...
        </div>
      </div>

      <div v-else class="space-y-6">
        <Card class="py-2 space-y-2 border-red-600/15 bg-red-600/8">
          <CardContent class="text-sm text-muted-foreground">
            Changing these will trigger updates to certificates, DNS records, and the Cloudflare
            tunnel. Existing connections can be disrupted until DNS updates propagate and clients
            refresh their configuration. Old records need to be cleaned up manually in Cloudflare
            after the change.
          </CardContent>
        </Card>

        <FieldSet>
          <FieldGroup>
            <Field>
              <FieldLabel for="cloudflare-domain">Base Domain</FieldLabel>
              <Input
                id="cloudflare-domain"
                :model-value="settings.domain ?? ''"
                @update:model-value="value => updateField('domain', value)"
                placeholder="example.com"
                :disabled="saving || isLoading"
              />
              <FieldDescription>
                Root DNS name used for core, internal, and public application routes.
              </FieldDescription>
            </Field>

            <Field>
              <FieldLabel for="cloudflare-admin-email">Admin Email</FieldLabel>
              <Input
                id="cloudflare-admin-email"
                :model-value="settings.admin_email ?? ''"
                @update:model-value="value => updateField('admin_email', value)"
                type="email"
                placeholder="admin@example.com"
                :disabled="saving || isLoading"
              />
              <FieldDescription>
                ACME contact email used by Traefik for certificate registration.
              </FieldDescription>
            </Field>

            <Field>
              <FieldLabel for="cloudflare-api-token">API Token</FieldLabel>
              <SecretInput
                id="cloudflare-api-token"
                :model-value="settings.api_token ?? ''"
                @update:model-value="value => updateField('api_token', value)"
                placeholder="Cloudflare API token"
                :disabled="saving || isLoading"
              />
              <FieldDescription>
                Token used to validate zones, manage tunnels, and upsert DNS records.
              </FieldDescription>
            </Field>

            <Field>
              <FieldLabel for="cloudflare-account-id">Account ID</FieldLabel>
              <Input
                id="cloudflare-account-id"
                :model-value="settings.account_id ?? ''"
                @update:model-value="value => updateField('account_id', value)"
                placeholder="Cloudflare account id"
                :disabled="saving || isLoading"
              />
              <FieldDescription>
                Cloudflare account identifier used for Zero Trust tunnel operations.
              </FieldDescription>
            </Field>

            <Field v-if="error">
              <FieldError>{{ error }}</FieldError>
            </Field>
          </FieldGroup>
        </FieldSet>
      </div>
    </CardContent>
    <CardFooter>
      <Button @click="onClickSave" :disabled="saving || isLoading">
        <Spinner v-if="saving" class="animate-spin" />
        {{ saving ? 'Saving...' : 'Save changes' }}
      </Button>
    </CardFooter>
  </Card>
</template>
