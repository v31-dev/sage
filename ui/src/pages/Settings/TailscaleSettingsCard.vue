<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
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
import ConfirmationButton from '@/components/ConfirmationButton.vue'
import { coerceSettingFields, loadSetting, saveSetting, type SettingFields } from './common'

const TAILSCALE_FIELD_KEYS = ['worker_tag'] as const

const isLoading = ref(true)
const saving = ref(false)
const fields = ref<SettingFields>(coerceSettingFields(TAILSCALE_FIELD_KEYS))
const savedWorkerTag = ref('')
const error = ref('')

// Changing an existing tag changes which machines count as workers, so it is confirmed.
const isChangingWorkerTag = computed(
  () =>
    savedWorkerTag.value !== '' &&
    (fields.value.worker_tag ?? '').trim().replace(/^tag:/, '') !== savedWorkerTag.value
)

async function load() {
  await loadSetting({
    settingKey: 'tailscale',
    fieldKeys: TAILSCALE_FIELD_KEYS,
    fields,
    isLoading,
    error,
    fallbackMessage: 'Failed to load Tailscale settings',
  })
  savedWorkerTag.value = fields.value.worker_tag ?? ''
}

function updateField(fieldKey: string, value: string | number | undefined) {
  fields.value[fieldKey] = value == null ? '' : String(value)
  error.value = ''
}

async function onClickSave() {
  await saveSetting({
    settingKey: 'tailscale',
    fieldKeys: TAILSCALE_FIELD_KEYS,
    fields,
    saving,
    error,
    fallbackMessage: 'Failed to save Tailscale settings',
    successMessage: 'Tailscale settings saved successfully',
  })
  if (!error.value) {
    savedWorkerTag.value = fields.value.worker_tag ?? ''
  }
}

async function onConfirmSave() {
  await onClickSave()
  if (error.value) {
    throw new Error(error.value)
  }
}

onMounted(() => {
  load()
})
</script>

<template>
  <Card class="overflow-hidden">
    <CardHeader>
      <CardTitle>Tailscale</CardTitle>
      <CardDescription
        >Workers are the machines on your tailnet that carry the worker tag.</CardDescription
      >
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
              <FieldLabel for="tailscale-worker-tag">Worker Tag</FieldLabel>
              <Input
                id="tailscale-worker-tag"
                :model-value="fields.worker_tag ?? ''"
                @update:model-value="value => updateField('worker_tag', value)"
                placeholder="worker"
                :disabled="saving || isLoading"
              />
              <FieldDescription>
                Tailscale tag that marks worker machines, with or without the
                <code>tag:</code> prefix. Leave empty to turn worker discovery off.
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
      <ConfirmationButton
        v-if="isChangingWorkerTag"
        trigger-text="Save changes"
        title="Change the worker tag?"
        body="Machines that don't carry the new tag stop counting as workers: they are marked offline, their containers are taken out of routing, and deploys or stops on them fail until the tag matches again."
        confirm-text="Change tag"
        :disabled="saving || isLoading"
        :loading="saving"
        :on-confirm="onConfirmSave"
      />
      <Button v-else @click="onClickSave" :disabled="saving || isLoading">
        <Spinner v-if="saving" class="animate-spin" />
        {{ saving ? 'Saving...' : 'Save changes' }}
      </Button>
    </CardFooter>
  </Card>
</template>
