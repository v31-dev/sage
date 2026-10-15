import { computed, onMounted, onUnmounted, ref } from 'vue'
import { onBeforeRouteLeave } from 'vue-router'
import { toast } from 'vue-sonner'
import { settingsAPI, type Setting, type SettingSection } from '@/services/api'
import { useSettingsStatusStore } from '@/stores/settingsStatus'

export type SettingFields = Record<string, string>

function fieldsFromValue(fieldKeys: readonly string[], value: Setting['value']) {
  const fields: SettingFields = {}
  fieldKeys.forEach(key => {
    const entry = value?.[key]
    fields[key] = entry == null ? '' : String(entry)
  })
  return fields
}

function errorMessage(error: unknown, fallback: string) {
  return error instanceof Error ? error.message : fallback
}

/**
 * One settings section's form: load, save, test (the current values, saved or not) and
 * reset. Failures are shown inline, successes as toasts. Leaving with unsaved changes asks
 * first (`leaveOpen` / `resolveLeave`, rendered by SettingsSection).
 */
export function useSettingForm(
  section: SettingSection,
  fieldKeys: readonly string[],
  name: string
) {
  const statusStore = useSettingsStatusStore()
  const fields = ref<SettingFields>(fieldsFromValue(fieldKeys, null))
  const saved = ref<SettingFields>(fieldsFromValue(fieldKeys, null))
  const isLoading = ref(true)
  const saving = ref(false)
  const testing = ref(false)
  const error = ref('')
  const dirty = computed(() => fieldKeys.some(key => fields.value[key] !== saved.value[key]))

  // Empty fields are sent as null; the domain is case-insensitive.
  const outgoingValue = computed(() => {
    const value: Record<string, string | null> = {}
    fieldKeys.forEach(key => {
      const trimmed = (fields.value[key] ?? '').trim()
      value[key] = trimmed === '' ? null : key === 'domain' ? trimmed.toLowerCase() : trimmed
    })
    return value
  })

  function updateField(key: string, value: string | number | undefined) {
    fields.value[key] = value == null ? '' : String(value)
    error.value = ''
  }

  async function load() {
    isLoading.value = true
    error.value = ''
    try {
      const data = (await settingsAPI.fetchOne(section)) as Setting
      saved.value = fieldsFromValue(fieldKeys, data.value)
      fields.value = { ...saved.value }
    } catch (err) {
      error.value = errorMessage(err, `Failed to load ${name} settings`)
    } finally {
      isLoading.value = false
    }
  }

  async function save() {
    saving.value = true
    error.value = ''
    try {
      const data = (await settingsAPI.update(section, { value: outgoingValue.value })) as Setting
      saved.value = fieldsFromValue(fieldKeys, data.value)
      fields.value = { ...saved.value }
      toast.success(`${name} settings saved`)
      statusStore.refresh()
    } catch (err) {
      error.value = errorMessage(err, `Failed to save ${name} settings`)
    } finally {
      saving.value = false
    }
  }

  async function test() {
    testing.value = true
    error.value = ''
    try {
      const result = (await settingsAPI.action(`${section}/test`, {
        value: outgoingValue.value,
      })) as unknown as { message: string }
      toast.success(result.message)
    } catch (err) {
      error.value = errorMessage(err, `${name} test failed`)
    } finally {
      testing.value = false
    }
  }

  function reset() {
    fields.value = { ...saved.value }
    error.value = ''
  }

  const leaveOpen = ref(false)
  let leaveResolve: ((leave: boolean) => void) | null = null

  onBeforeRouteLeave(() => {
    if (!dirty.value) return true
    leaveOpen.value = true
    return new Promise<boolean>(resolve => {
      leaveResolve = resolve
    })
  })

  function resolveLeave(leave: boolean) {
    leaveOpen.value = false
    leaveResolve?.(leave)
    leaveResolve = null
  }

  function onBeforeUnload(event: BeforeUnloadEvent) {
    if (dirty.value) event.preventDefault()
  }

  onMounted(() => {
    load()
    window.addEventListener('beforeunload', onBeforeUnload)
  })
  onUnmounted(() => window.removeEventListener('beforeunload', onBeforeUnload))

  return {
    fields,
    saved,
    isLoading,
    saving,
    testing,
    error,
    dirty,
    updateField,
    save,
    test,
    reset,
    leaveOpen,
    resolveLeave,
  }
}
