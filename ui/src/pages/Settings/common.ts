import type { Ref } from 'vue'
import { toast } from 'vue-sonner'
import { settingsAPI, type Setting } from '@/services/api'

export type SettingFields = Record<string, string>

function settingFieldsFromValue(value: Setting['value']) {
  const fieldValues: SettingFields = {}

  if (value && typeof value === 'object') {
    Object.entries(value).forEach(([key, entryValue]) => {
      fieldValues[key] = entryValue === null ? '' : String(entryValue)
    })
  }

  return fieldValues
}

export function coerceSettingFields(fieldKeys: readonly string[], fields: SettingFields = {}) {
  const nextFields: SettingFields = {}

  fieldKeys.forEach(fieldKey => {
    nextFields[fieldKey] = fields[fieldKey] ?? ''
  })

  return nextFields
}

function normalizeOutgoingValue(settingKey: string, fieldKey: string, value: string) {
  const trimmedValue = value.trim()

  if (trimmedValue === '') {
    return null
  }

  if (settingKey === 'cloudflare' && fieldKey === 'domain') {
    return trimmedValue.toLowerCase()
  }

  return trimmedValue
}

function getErrorMessage(error: unknown, fallback: string) {
  return error instanceof Error ? error.message : fallback
}

interface LoadSettingOptions {
  settingKey: string
  fieldKeys: readonly string[]
  fields: Ref<SettingFields>
  isLoading: Ref<boolean>
  error: Ref<string>
  fallbackMessage: string
}

export async function loadSetting({
  settingKey,
  fieldKeys,
  fields,
  isLoading,
  error,
  fallbackMessage,
}: LoadSettingOptions) {
  error.value = ''

  try {
    isLoading.value = true
    const data = (await settingsAPI.fetchOne(settingKey)) as Setting
    fields.value = coerceSettingFields(fieldKeys, settingFieldsFromValue(data.value))
  } catch (err) {
    console.error('Failed to load settings:', err)
    error.value = getErrorMessage(err, fallbackMessage)
  } finally {
    isLoading.value = false
  }
}

interface SaveSettingOptions {
  settingKey: string
  fieldKeys: readonly string[]
  fields: Ref<SettingFields>
  saving: Ref<boolean>
  error: Ref<string>
  fallbackMessage: string
  successMessage: string
}

export async function saveSetting({
  settingKey,
  fieldKeys,
  fields,
  saving,
  error,
  fallbackMessage,
  successMessage,
}: SaveSettingOptions) {
  error.value = ''

  try {
    saving.value = true

    const value: Record<string, string | null> = {}
    fieldKeys.forEach(fieldKey => {
      value[fieldKey] = normalizeOutgoingValue(settingKey, fieldKey, fields.value[fieldKey] ?? '')
    })

    const data = (await settingsAPI.update(settingKey, { value })) as Setting
    fields.value = coerceSettingFields(fieldKeys, settingFieldsFromValue(data.value))
    toast.success(successMessage)
  } catch (err) {
    error.value = getErrorMessage(err, fallbackMessage)
  } finally {
    saving.value = false
  }
}
