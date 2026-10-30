<script lang="ts">
// A list's filter: a dropdown of choices, with one for any, or a field.
export interface FilterDropdown {
  key: string
  label: string
  any: string
  options: string[]
}

export interface FilterField {
  key: string
  label: string
  type: 'text' | 'number' | 'datetime-local'
  placeholder?: string
}
</script>

<script setup lang="ts" generic="Filters extends Record<string, string>">
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'

// The filters of a list, by key, '' for none. A dropdown applies when chosen, a field once it is committed (Enter, or
// leaving it), not at each key. `id` names the controls: `<id>-<key>`.
const props = defineProps<{ id: string; dropdowns?: FilterDropdown[]; fields?: FilterField[] }>()
const filters = defineModel<Filters>({ required: true })

// A dropdown's choice for no filter, since a select item cannot have an empty value.
const ANY = 'any'

// A new value only: committing a field again (Enter, then leaving it) changes nothing.
function set(key: string, value: string) {
  if (filters.value[key] === value) return
  filters.value = { ...filters.value, [key]: value } as Filters
}
</script>

<template>
  <div class="grid gap-3 grid-cols-2 sm:grid-cols-4">
    <div v-for="dropdown in props.dropdowns" :key="dropdown.key" class="space-y-1">
      <Label :for="`${props.id}-${dropdown.key}`" class="text-xs">{{ dropdown.label }}</Label>
      <Select
        :model-value="filters[dropdown.key] || ANY"
        @update:model-value="value => set(dropdown.key, value === ANY ? '' : String(value))"
      >
        <SelectTrigger :id="`${props.id}-${dropdown.key}`" class="w-full">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          <SelectItem :value="ANY">{{ dropdown.any }}</SelectItem>
          <SelectItem v-for="option in dropdown.options" :key="option" :value="option">
            {{ option }}
          </SelectItem>
        </SelectContent>
      </Select>
    </div>
    <div v-for="field in props.fields" :key="field.key" class="space-y-1">
      <Label :for="`${props.id}-${field.key}`" class="text-xs">{{ field.label }}</Label>
      <Input
        :id="`${props.id}-${field.key}`"
        :type="field.type"
        :placeholder="field.placeholder"
        :min="field.type === 'number' ? 0 : undefined"
        :model-value="filters[field.key]"
        @change="set(field.key, ($event.target as HTMLInputElement).value.trim())"
      />
    </div>
  </div>
</template>
