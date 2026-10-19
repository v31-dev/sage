<script setup lang="ts">
import { ref } from 'vue'
import { Eye, EyeOff } from 'lucide-vue-next'
import {
  InputGroup,
  InputGroupAddon,
  InputGroupButton,
  InputGroupInput,
  InputGroupTextarea,
} from '@/components/ui/input-group'
import CopyButton from '@/components/CopyButton.vue'

interface Props {
  secret?: boolean
  type?: 'input' | 'textarea'
}

defineOptions({ inheritAttrs: false })

const props = withDefaults(defineProps<Props>(), {
  secret: false,
  type: 'input',
})

const modelValue = defineModel<string | number>()
const revealed = ref<boolean>(!props.secret || false)
</script>

<template>
  <InputGroup>
    <InputGroupInput
      v-if="props.type === 'input'"
      v-bind="$attrs"
      v-model="modelValue"
      :style="!revealed ? { '-webkit-text-security': 'disc', textSecurity: 'disc' } : {}"
      autocomplete="off"
    />
    <InputGroupTextarea
      v-if="props.type === 'textarea'"
      v-bind="$attrs"
      v-model="modelValue"
      :style="!revealed ? { '-webkit-text-security': 'disc', textSecurity: 'disc' } : {}"
    />
    <InputGroupAddon align="inline-end">
      <!-- The masking is only painted, so selecting the text and copying it takes the bullets. -->
      <CopyButton v-if="props.secret && modelValue" compact :text="String(modelValue)" />
      <InputGroupButton v-if="props.secret" size="icon-xs" @click="revealed = !revealed">
        <Eye v-if="!revealed" />
        <EyeOff v-else />
      </InputGroupButton>
    </InputGroupAddon>
  </InputGroup>
</template>
