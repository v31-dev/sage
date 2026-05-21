<script setup lang="ts">
import { ref } from 'vue'
import { Eye, EyeOff, InfoIcon } from 'lucide-vue-next'
import {
  InputGroup,
  InputGroupAddon,
  InputGroupButton,
  InputGroupInput,
} from '@/components/ui/input-group'
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '@/components/ui/tooltip'

interface Props {
  secret?: boolean
  tooltip?: string
}

const props = withDefaults(defineProps<Props>(), {
  secret: false,
  tooltip: '',
})

const modelValue = defineModel<string | number>()
const revealed = ref<boolean>(!props.secret || false)
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
      <InputGroupButton v-if="props.secret" size="icon-xs" @click="revealed = !revealed">
        <Eye v-if="!revealed" />
        <EyeOff v-else />
      </InputGroupButton>
    </InputGroupAddon>
  </InputGroup>
</template>
