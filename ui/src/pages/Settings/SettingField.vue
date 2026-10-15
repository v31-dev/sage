<script setup lang="ts">
import { Field, FieldContent, FieldDescription, FieldLabel } from '@/components/ui/field'

defineProps<{ id: string; label: string; description?: string }>()
</script>

<template>
  <!-- Label and help on the left, the control on the right when the form is wide enough;
       stacked when narrow. The control keeps a fixed share so the inputs line up. -->
  <Field orientation="responsive" class="@md/field-group:[&>:last-child]:min-w-[55%]">
    <FieldContent>
      <FieldLabel :for="id">{{ label }}</FieldLabel>
      <FieldDescription v-if="description">{{ description }}</FieldDescription>
    </FieldContent>
    <slot />
  </Field>
</template>
