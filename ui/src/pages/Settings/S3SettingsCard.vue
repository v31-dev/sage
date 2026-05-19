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

const S3_FIELD_KEYS = ['access_key', 'secret_key', 'bucket', 'endpoint', 'path'] as const

const isLoading = ref(true)
const saving = ref(false)
const fields = ref<SettingFields>(coerceSettingFields(S3_FIELD_KEYS))
const error = ref('')

const load = () =>
  loadSetting({
    settingKey: 's3',
    fieldKeys: S3_FIELD_KEYS,
    fields,
    isLoading,
    error,
    fallbackMessage: 'Failed to load S3 settings',
  })

function updateField(fieldKey: string, value: string | number) {
  fields.value[fieldKey] = String(value)
  error.value = ''
}

const onClickSave = () =>
  saveSetting({
    settingKey: 's3',
    fieldKeys: S3_FIELD_KEYS,
    fields,
    saving,
    error,
    fallbackMessage: 'Failed to save S3 settings',
    successMessage: 'S3 settings saved successfully',
  })

onMounted(() => {
  load()
})
</script>

<template>
  <Card class="overflow-hidden">
    <CardHeader>
      <CardTitle>S3 Backups</CardTitle>
      <CardDescription>Object storage target for platform and application backups.</CardDescription>
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
        <FieldSet>
          <FieldGroup>
            <Field>
              <FieldLabel for="s3-access-key">Access Key</FieldLabel>
              <Input
                id="s3-access-key"
                :model-value="fields.access_key ?? ''"
                @update:model-value="value => updateField('access_key', value)"
                placeholder="S3 access key"
                :disabled="saving || isLoading"
              />
              <FieldDescription>Access key for the S3-compatible bucket.</FieldDescription>
            </Field>

            <Field>
              <FieldLabel for="s3-secret-key">Secret Key</FieldLabel>
              <SecretInput
                id="s3-secret-key"
                :model-value="fields.secret_key ?? ''"
                @update:model-value="value => updateField('secret_key', value)"
                placeholder="S3 secret key"
                :disabled="saving || isLoading"
              />
              <FieldDescription>Secret key paired with the access key.</FieldDescription>
            </Field>

            <Field>
              <FieldLabel for="s3-bucket">Bucket</FieldLabel>
              <Input
                id="s3-bucket"
                :model-value="fields.bucket ?? ''"
                @update:model-value="value => updateField('bucket', value)"
                placeholder="sage-backups"
                :disabled="saving || isLoading"
              />
              <FieldDescription>Bucket name used for backups.</FieldDescription>
            </Field>

            <Field>
              <FieldLabel for="s3-endpoint">Endpoint</FieldLabel>
              <Input
                id="s3-endpoint"
                :model-value="fields.endpoint ?? ''"
                @update:model-value="value => updateField('endpoint', value)"
                type="url"
                placeholder="https://..."
                :disabled="saving || isLoading"
              />
              <FieldDescription>S3 or S3-compatible HTTPS endpoint.</FieldDescription>
            </Field>

            <Field>
              <FieldLabel for="s3-path">Base Path</FieldLabel>
              <Input
                id="s3-path"
                :model-value="fields.path ?? ''"
                @update:model-value="value => updateField('path', value)"
                placeholder="/sage"
                :disabled="saving || isLoading"
              />
              <FieldDescription>
                Prefix inside the bucket where Sage stores all backup objects.
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
