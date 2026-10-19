<script setup lang="ts">
import { ref } from 'vue'
import { Eye, EyeOff, InfoIcon } from 'lucide-vue-next'
import {
  InputGroup,
  InputGroupAddon,
  InputGroupButton,
  InputGroupInput,
  InputGroupTextarea,
} from '@/components/ui/input-group'
import CopyButton from '@/components/CopyButton.vue'
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '@/components/ui/tooltip'

interface Props {
  secret?: boolean
  tooltip?: string
  type?: 'input' | 'textarea'
}

// Attributes (id, placeholder, class…) belong on the inner input, not the group wrapper.
defineOptions({ inheritAttrs: false })

const props = withDefaults(defineProps<Props>(), {
  secret: false,
  tooltip: '',
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
      <TooltipProvider v-if="props.tooltip !== ''">
        <Tooltip>
          <TooltipTrigger as-child>
            <InputGroupButton class="rounded-full" size="icon-xs">
              <InfoIcon />
            </InputGroupButton>
          </TooltipTrigger>
          <TooltipContent>
            {{ props.tooltip }}
          </TooltipContent>
        </Tooltip>
      </TooltipProvider>
      <!-- The masking is only painted, so selecting the text and copying it takes the bullets. -->
      <CopyButton v-if="props.secret && modelValue" compact :text="String(modelValue)" />
      <InputGroupButton v-if="props.secret" size="icon-xs" @click="revealed = !revealed">
        <Eye v-if="!revealed" />
        <EyeOff v-else />
      </InputGroupButton>
    </InputGroupAddon>
  </InputGroup>
</template>
