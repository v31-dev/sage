<script setup lang="ts">
import { ref } from 'vue'
import { Eye, EyeOff } from 'lucide-vue-next'
import { InputGroup, InputGroupAddon, InputGroupButton } from '@/components/ui/input-group'
import InputGroupInput from './ui/input-group/InputGroupInput.vue'

const modelValue = defineModel<string | number>()
const revealed = ref<boolean>(false)
</script>

<template>
  <InputGroup>
    <InputGroupInput
      v-bind="$attrs"
      v-model="modelValue"
      :type="revealed ? 'text' : 'password'"
      autocomplete="off"
    />
    <InputGroupAddon align="inline-end">
      <InputGroupButton size="icon-xs" @click="revealed = !revealed">
        <Eye v-if="!revealed" />
        <EyeOff v-else />
      </InputGroupButton>
    </InputGroupAddon>
  </InputGroup>
</template>
