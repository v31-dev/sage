<script setup lang="ts">
import { ref } from 'vue'
import { Eye, EyeOff } from 'lucide-vue-next'
import { InputGroup, InputGroupAddon, InputGroupButton } from '@/components/ui/input-group'
import InputGroupInput from './ui/input-group/InputGroupInput.vue'

const props = defineProps<{
  defaultValue?: string | number
  modelValue?: string | number
}>()

const emits = defineEmits<{
  (e: 'update:modelValue', payload: string | number): void
}>()

const revealed = ref<boolean>(false)
</script>

<template>
  <InputGroup>
    <InputGroupInput
      v-bind="$attrs"
      :model-value="props.modelValue"
      :type="revealed ? 'text' : 'password'"
      autocomplete="new-password"
      @update:model-value="value => $emit('update:modelValue', value)"
    />
    <InputGroupAddon align="inline-end">
      <InputGroupButton size="icon-xs" @click="revealed = !revealed">
        <Eye v-if="!revealed" />
        <EyeOff v-else />
      </InputGroupButton>
    </InputGroupAddon>
  </InputGroup>
</template>
