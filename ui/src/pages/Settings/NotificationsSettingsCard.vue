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

const NOTIFICATION_FIELD_KEYS = ['discord_webhook'] as const

const isLoading = ref(true)
const saving = ref(false)
const fields = ref<SettingFields>(coerceSettingFields(NOTIFICATION_FIELD_KEYS))
const error = ref('')

const load = () =>
  loadSetting({
    settingKey: 'notifications',
    fieldKeys: NOTIFICATION_FIELD_KEYS,
    fields,
    isLoading,
    error,
    fallbackMessage: 'Failed to load notification settings',
  })

function updateField(fieldKey: string, value: string | number) {
  fields.value[fieldKey] = String(value)
  error.value = ''
}

const onClickSave = () =>
  saveSetting({
    settingKey: 'notifications',
    fieldKeys: NOTIFICATION_FIELD_KEYS,
    fields,
    saving,
    error,
    fallbackMessage: 'Failed to save notification settings',
    successMessage: 'Notification settings saved successfully',
  })

onMounted(() => {
  load()
})
</script>

<template>
  <Card class="overflow-hidden">
    <CardHeader>
      <CardTitle>Notifications</CardTitle>
      <CardDescription>Optional outbound notifications for operational events.</CardDescription>
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

      <template v-else>
        <FieldSet>
          <FieldGroup>
            <Field>
              <FieldLabel for="notifications-discord-webhook">Discord Webhook</FieldLabel>
              <Input
                id="notifications-discord-webhook"
                :model-value="fields.discord_webhook ?? ''"
                @update:model-value="value => updateField('discord_webhook', value)"
                type="url"
                placeholder="https://discord.com/api/webhooks/..."
                :disabled="saving || isLoading"
              />
              <FieldDescription>
                Webhook endpoint used for async notification delivery.
              </FieldDescription>
            </Field>

            <Field v-if="error">
              <FieldError>{{ error }}</FieldError>
            </Field>
          </FieldGroup>
        </FieldSet>
      </template>
    </CardContent>
    <CardFooter>
      <Button @click="onClickSave" :disabled="saving || isLoading">
        <Spinner v-if="saving" class="animate-spin" />
        {{ saving ? 'Saving...' : 'Save changes' }}
      </Button>
    </CardFooter>
  </Card>
</template>
