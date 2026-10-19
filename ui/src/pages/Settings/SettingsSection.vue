<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { onBeforeRouteLeave } from 'vue-router'
import { toast } from 'vue-sonner'
import { Lock, OctagonX, Undo2 } from 'lucide-vue-next'
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
import { Input } from '@/components/ui/input'
import { InputGroup, InputGroupAddon, InputGroupInput } from '@/components/ui/input-group'
import { Spinner } from '@/components/ui/spinner'
import CopyButton from '@/components/CopyButton.vue'
import CustomInput from '@/components/CustomInput.vue'
import StateIndicator from '@/components/StateIndicator.vue'
import SettingField from './SettingField.vue'
import { settingIndicator, settingsAPI, type Setting, type SettingSection } from '@/services/api'
import { useAppStore } from '@/stores/app'

type SettingValues = Record<string, string>

interface FieldDefinition {
  key: string
  label: string
  description?: string
  placeholder?: string
  type?: string
  secret?: boolean
  // Shown as-is and never loaded or saved; `value` is what it shows.
  readonly?: boolean
  value?: string
}

const props = withDefaults(
  defineProps<{
    title: string
    description: string
    section: SettingSection
    fields?: readonly FieldDefinition[]
    testLabel?: string
  }>(),
  {
    fields: () => [],
    testLabel: 'Test',
  }
)

defineSlots<{
  save(): unknown
  links(): unknown
  after(): unknown
}>()

const appStore = useAppStore()
const status = computed(() => appStore.info?.settings?.[props.section])

const keys = computed(() => props.fields.filter(field => !field.readonly).map(field => field.key))
const editable = computed(() => keys.value.length > 0)

function fieldId(key: string) {
  return `${props.section}-${key.replace(/_/g, '-')}`
}

function valuesFrom(value: Setting['value']) {
  const result: SettingValues = {}
  keys.value.forEach(key => {
    const entry = value?.[key]
    result[key] = entry == null ? '' : String(entry)
  })
  return result
}

function errorMessage(err: unknown, fallback: string) {
  return err instanceof Error ? err.message : fallback
}

const values = ref<SettingValues>(valuesFrom(null))
const saved = ref<SettingValues>(valuesFrom(null))
const isLoading = ref(editable.value)
const saving = ref(false)
const testing = ref(false)
const error = ref('')
const dirty = computed(() => keys.value.some(key => values.value[key] !== saved.value[key]))

// Empty fields are sent as null.
const outgoingValue = computed(() => {
  const value: Record<string, string | null> = {}
  keys.value.forEach(key => {
    const trimmed = (values.value[key] ?? '').trim()
    value[key] = trimmed === '' ? null : trimmed
  })
  return value
})

function updateField(key: string, value: string | number | undefined) {
  values.value[key] = value == null ? '' : String(value)
  error.value = ''
}

async function load() {
  isLoading.value = true
  error.value = ''
  try {
    const data = (await settingsAPI.fetchOne(props.section)) as Setting
    saved.value = valuesFrom(data.value)
    values.value = { ...saved.value }
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
    saved.value = valuesFrom(data.value)
    values.value = { ...saved.value }
    toast.success(`${props.title} settings saved`)
    // A saved section is checked again, unless it was emptied.
    if (keys.value.some(key => saved.value[key] !== '')) appStore.setSettingApplying(props.section)
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
  values.value = { ...saved.value }
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
  if (!editable.value) return
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
            <StateIndicator v-bind="settingIndicator(status.state)" show-label />
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
              <SettingField
                v-for="field in fields"
                :key="field.key"
                :id="fieldId(field.key)"
                :label="field.label"
                :description="field.description"
                :error="status?.channels?.[field.key]"
              >
                <InputGroup v-if="field.readonly">
                  <InputGroupAddon><Lock /></InputGroupAddon>
                  <InputGroupInput :id="fieldId(field.key)" :model-value="field.value" readonly />
                  <InputGroupAddon v-if="field.value" align="inline-end">
                    <CopyButton compact :text="field.value" />
                  </InputGroupAddon>
                </InputGroup>
                <CustomInput
                  v-else-if="field.secret"
                  secret
                  :id="fieldId(field.key)"
                  :model-value="values[field.key]"
                  @update:model-value="value => updateField(field.key, value)"
                  :placeholder="field.placeholder"
                />
                <Input
                  v-else
                  :id="fieldId(field.key)"
                  :type="field.type"
                  :model-value="values[field.key]"
                  @update:model-value="value => updateField(field.key, value)"
                  :placeholder="field.placeholder"
                />
              </SettingField>
            </FieldGroup>
          </FieldSet>

          <Alert v-if="error" variant="destructive">
            <OctagonX />
            <AlertDescription>{{ error }}</AlertDescription>
          </Alert>
        </CardContent>

        <CardFooter v-if="editable || $slots.links" class="flex flex-wrap gap-2">
          <template v-if="editable">
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
          </template>
          <slot name="links" />
        </CardFooter>
      </Card>

      <slot name="after" />
    </div>

    <AlertDialog v-if="editable" :open="leaveOpen">
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
