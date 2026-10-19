<script setup lang="ts">
import { FieldLabel } from '@/components/ui/field'
import FieldHelp from '@/components/FieldHelp.vue'

// A field's label with its help beside it, not inside the input.
defineProps<{ for: string; help: string }>()
</script>

<template>
  <div class="flex items-center gap-1.5">
    <FieldLabel :for="$props.for"><slot /></FieldLabel>
    <FieldHelp :text="help" />
  </div>
</template>
