<script setup lang="ts">
import { computed, ref } from 'vue'
import { toast } from 'vue-sonner'
import { Edit, Plus, Trash } from 'lucide-vue-next'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from '@/components/ui/dialog'
import { Field, FieldError, FieldGroup, FieldLabel, FieldSet } from '@/components/ui/field'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Spinner } from '@/components/ui/spinner'
import { Switch } from '@/components/ui/switch'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import { Textarea } from '@/components/ui/textarea'
import FieldLabelHelp from '@/components/FieldLabelHelp.vue'
import IconButton from '@/components/IconButton.vue'
import SectionTitle from '@/components/SectionTitle.vue'
import { type Application, updateApplication } from '@/services/api'

const props = defineProps<{
  application: Application
  loadApplication: () => Promise<void>
}>()

const STATES = {
  default: 'Default',
  changed: 'Changed',
  own: "Application's own",
  added: 'Added',
} as const

// What each header sends: Sage's default, a change of it, the application's own, or a header it adds.
const rows = computed(() => {
  const { defaults, changes } = props.application.response_headers
  return [
    ...Object.entries(defaults).map(([name, value]) => {
      if (!(name in changes)) return { name, value, state: 'default' as const }
      const change = changes[name]
      return change === null
        ? { name, value: 'Sent by the application', state: 'own' as const }
        : { name, value: change!, state: 'changed' as const }
    }),
    ...Object.entries(changes)
      .filter(([name]) => !(name in defaults))
      .map(([name, value]) => ({ name, value: value ?? '', state: 'added' as const })),
  ]
})

const isOpen = ref(false)
const isSaving = ref(false)
const errorMessage = ref('')
const defaultsForm = ref<{ name: string; value: string; own: boolean }[]>([])
const ownForm = ref<{ name: string; value: string }[]>([])

function open() {
  const { defaults, changes } = props.application.response_headers
  defaultsForm.value = Object.entries(defaults).map(([name, value]) => ({
    name,
    value: changes[name] ?? value,
    own: changes[name] === null,
  }))
  ownForm.value = Object.entries(changes)
    .filter(([name]) => !(name in defaults))
    .map(([name, value]) => ({ name, value: value ?? '' }))
  errorMessage.value = ''
  isOpen.value = true
}

const mayBeOwn = (name: string) => props.application.response_headers.own_allowed.includes(name)

function reset(row: { name: string; value: string; own: boolean }) {
  row.value = props.application.response_headers.defaults[row.name]!
  row.own = false
}

const isDefault = (row: { name: string; value: string; own: boolean }) =>
  !row.own && row.value.trim() === props.application.response_headers.defaults[row.name]

// A header left blank sends its default; one the application sends itself is saved as null.
async function save() {
  const changes: Record<string, string | null> = {}
  for (const row of defaultsForm.value) {
    if (row.own) changes[row.name] = null
    else if (row.value.trim() && !isDefault(row)) changes[row.name] = row.value
  }
  for (const row of ownForm.value) {
    if (row.name.trim() || row.value.trim()) changes[row.name.trim()] = row.value
  }
  errorMessage.value = ''
  isSaving.value = true
  try {
    await updateApplication(props.application.project.name, props.application.name, {
      response_headers: Object.keys(changes).length ? changes : null,
    })
    await props.loadApplication()
    isOpen.value = false
    toast.success('Response headers updated')
  } catch (err) {
    errorMessage.value =
      err instanceof Error ? err.message : 'Failed to update the response headers'
  } finally {
    isSaving.value = false
  }
}
</script>

<template>
  <div class="space-y-4">
    <div class="flex justify-between items-center">
      <SectionTitle
        title="Response Headers"
        :size="4"
        tooltip="Sent by every HTTP name of this application. Strict by default: change a header to allow what the application needs."
      />
      <Dialog v-model:open="isOpen">
        <DialogTrigger asChild>
          <IconButton :icon="Edit" label="Edit" @click="open" />
        </DialogTrigger>
        <DialogContent class="sm:max-w-[720px] max-h-[90vh] overflow-y-auto">
          <DialogHeader>
            <DialogTitle>Edit Response Headers</DialogTitle>
            <DialogDescription>
              Strict by default. Change a header to allow what the application needs.
            </DialogDescription>
          </DialogHeader>
          <FieldSet>
            <FieldGroup>
              <Field v-for="row in defaultsForm" :key="row.name" :data-header="row.name">
                <FieldLabel :for="`header-${row.name}`">{{ row.name }}</FieldLabel>
                <div v-if="mayBeOwn(row.name)" class="flex items-center gap-2">
                  <Switch :id="`header-${row.name}-own`" v-model="row.own" />
                  <Label :for="`header-${row.name}-own`">The application sends its own</Label>
                </div>
                <Textarea
                  v-if="!row.own"
                  :id="`header-${row.name}`"
                  v-model="row.value"
                  rows="2"
                  class="resize-none font-mono text-xs"
                />
                <div>
                  <Button
                    v-if="!isDefault(row)"
                    size="sm"
                    variant="ghost"
                    type="button"
                    @click="reset(row)"
                  >
                    Reset to default
                  </Button>
                </div>
              </Field>
              <Field>
                <FieldLabelHelp
                  help="Only headers whose names start with X- are kept: any other is dropped when you save."
                >
                  Your own headers (optional)
                </FieldLabelHelp>
                <div
                  v-for="(row, index) in ownForm"
                  :key="index"
                  class="flex gap-2 items-center"
                  data-own-header
                >
                  <Input v-model="row.name" placeholder="e.g. X-App-Version" class="w-1/3" />
                  <Input v-model="row.value" placeholder="e.g. 1.4.2" />
                  <IconButton :icon="Trash" label="Remove" @click="ownForm.splice(index, 1)" />
                </div>
                <div>
                  <Button
                    size="sm"
                    variant="outline"
                    type="button"
                    @click="ownForm.push({ name: '', value: '' })"
                  >
                    <Plus /> Add header
                  </Button>
                </div>
              </Field>
              <Field>
                <FieldError v-if="errorMessage">{{ errorMessage }}</FieldError>
              </Field>
            </FieldGroup>
          </FieldSet>
          <DialogFooter>
            <Button
              size="sm"
              type="button"
              variant="outline"
              :disabled="isSaving"
              @click="isOpen = false"
            >
              Cancel
            </Button>
            <Button size="sm" :disabled="isSaving" @click="save">
              <Spinner v-if="isSaving" class="animate-spin" />
              Save
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
    <Card>
      <CardContent>
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Header</TableHead>
              <TableHead>Value</TableHead>
              <TableHead />
            </TableRow>
          </TableHeader>
          <TableBody>
            <TableRow v-for="row in rows" :key="row.name" :data-row="row.name">
              <TableCell class="font-medium whitespace-nowrap align-top">{{ row.name }}</TableCell>
              <TableCell class="font-mono text-xs whitespace-normal break-all">
                {{ row.value }}
              </TableCell>
              <TableCell class="text-right align-top">
                <Badge :variant="row.state === 'default' ? 'outline' : 'secondary'">
                  {{ STATES[row.state] }}
                </Badge>
              </TableCell>
            </TableRow>
          </TableBody>
        </Table>
      </CardContent>
    </Card>
  </div>
</template>
