<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { onBeforeRouteLeave } from 'vue-router'
import { toast } from 'vue-sonner'
import { OctagonX, Undo2 } from 'lucide-vue-next'
import { Alert, AlertDescription } from '@/components/ui/alert'
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog'
import { Button } from '@/components/ui/button'
import {
  Card,
  CardAction,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from '@/components/ui/card'
import { FieldGroup, FieldSet } from '@/components/ui/field'
import { Spinner } from '@/components/ui/spinner'
import SettingStateIndicator from '@/components/SettingStateIndicator.vue'
import { settingsAPI, type Setting, type SettingSection } from '@/services/api'
import { useSettingsStatusStore } from '@/stores/settingsStatus'

type SettingFields = Record<string, string>

const props = withDefaults(
  defineProps<{
    title: string
    description: string
    section: SettingSection
    fieldKeys?: readonly string[]
    readonly?: boolean
    testLabel?: string
  }>(),
  {
    fieldKeys: () => [],
    readonly: false,
    testLabel: 'Test',
  }
)

defineSlots<{
  default(props: {
    fields: SettingFields
    updateField: (key: string, value: string | number | undefined) => void
  }): unknown
  save(): unknown
  links(): unknown
  after(): unknown
}>()

const statusStore = useSettingsStatusStore()
const status = computed(() => statusStore.status?.[props.section])

function fieldsFromValue(value: Setting['value']) {
  const values: SettingFields = {}
  props.fieldKeys.forEach(key => {
    const entry = value?.[key]
    values[key] = entry == null ? '' : String(entry)
  })
  return values
}

function errorMessage(err: unknown, fallback: string) {
  return err instanceof Error ? err.message : fallback
}

const fields = ref<SettingFields>(fieldsFromValue(null))
const saved = ref<SettingFields>(fieldsFromValue(null))
const isLoading = ref(!props.readonly)
const saving = ref(false)
const testing = ref(false)
const error = ref('')
const dirty = computed(() => props.fieldKeys.some(key => fields.value[key] !== saved.value[key]))

// Empty fields are sent as null.
const outgoingValue = computed(() => {
  const value: Record<string, string | null> = {}
  props.fieldKeys.forEach(key => {
    const trimmed = (fields.value[key] ?? '').trim()
    value[key] = trimmed === '' ? null : trimmed
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
    const data = (await settingsAPI.fetchOne(props.section)) as Setting
    saved.value = fieldsFromValue(data.value)
    fields.value = { ...saved.value }
  } catch (err) {
    error.value = errorMessage(err, `Failed to load ${props.title} settings`)
  } finally {
    isLoading.value = false
  }
}

async function save() {
  saving.value = true
  error.value = ''
  try {
    const data = (await settingsAPI.update(props.section, {
      value: outgoingValue.value,
    })) as Setting
    saved.value = fieldsFromValue(data.value)
    fields.value = { ...saved.value }
    toast.success(`${props.title} settings saved`)
    statusStore.refresh()
  } catch (err) {
    error.value = errorMessage(err, `Failed to save ${props.title} settings`)
  } finally {
    saving.value = false
  }
}

async function test() {
  testing.value = true
  error.value = ''
  try {
    const result = (await settingsAPI.action(`${props.section}/test`, {
      value: outgoingValue.value,
    })) as unknown as { message: string }
    toast.success(result.message)
  } catch (err) {
    error.value = errorMessage(err, `${props.title} test failed`)
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
  if (props.readonly) return
  load()
  window.addEventListener('beforeunload', onBeforeUnload)
})
onUnmounted(() => window.removeEventListener('beforeunload', onBeforeUnload))
</script>

<template>
  <main class="flex-1 px-2 sm:px-4 py-4">
    <div class="max-w-3xl mx-auto flex flex-col gap-6">
      <Card>
        <CardHeader>
          <CardTitle>{{ title }}</CardTitle>
          <CardDescription>{{ description }}</CardDescription>
          <CardAction v-if="status">
            <SettingStateIndicator :state="status.state" show-label />
          </CardAction>
        </CardHeader>

        <CardContent class="space-y-6">
          <p v-if="status?.message" class="text-sm text-muted-foreground">
            {{ status.message }}
          </p>

          <div v-if="isLoading" class="flex items-center gap-3 py-8 text-sm text-muted-foreground">
            <Spinner class="animate-spin" />
            Loading settings...
          </div>
          <FieldSet v-else :disabled="saving">
            <FieldGroup>
              <slot :fields="fields" :updateField="updateField" />
            </FieldGroup>
          </FieldSet>

          <Alert v-if="error" variant="destructive">
            <OctagonX />
            <AlertDescription>{{ error }}</AlertDescription>
          </Alert>
        </CardContent>

        <CardFooter v-if="readonly && $slots.links" class="flex flex-wrap gap-2">
          <slot name="links" />
        </CardFooter>
        <CardFooter v-else-if="!readonly" class="flex flex-wrap gap-2">
          <slot name="save">
            <Button :disabled="saving || isLoading" @click="save()">
              <Spinner v-if="saving" class="animate-spin" />
              Save changes
            </Button>
          </slot>
          <Button variant="outline" :disabled="testing || isLoading" @click="test()">
            <Spinner v-if="testing" class="animate-spin" />
            {{ testLabel }}
          </Button>
          <Button v-if="dirty" variant="ghost" :disabled="saving" @click="reset()">
            <Undo2 />
            Reset
          </Button>
        </CardFooter>
      </Card>

      <slot name="after" />
    </div>

    <AlertDialog v-if="!readonly" :open="leaveOpen">
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>Discard unsaved changes?</AlertDialogTitle>
          <AlertDialogDescription>
            Your changes to the {{ title }} settings haven't been saved.
          </AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel @click="resolveLeave(false)">Stay</AlertDialogCancel>
          <AlertDialogAction @click="resolveLeave(true)">Discard changes</AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  </main>
</template>
