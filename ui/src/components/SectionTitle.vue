<script setup lang="ts">
import { computed, ref } from 'vue'
import { Tooltip, TooltipContent, TooltipTrigger, TooltipProvider } from '@/components/ui/tooltip'
import { InputGroupButton } from '@/components/ui/input-group'
import { InfoIcon } from 'lucide-vue-next'

// A heading with an optional info tip. The default slot is for the state shown beside it.
interface Props {
  title?: string
  tooltip?: string
  size?: 1 | 2 | 3 | 4 | 5 | 6
}

const props = withDefaults(defineProps<Props>(), {
  title: '',
  tooltip: '',
  size: 6,
})

const isTooltipOpen = ref(false)

const headingSize = computed(() => {
  const sizes: Record<number, string> = {
    1: 'text-4xl',
    2: 'text-3xl',
    3: 'text-2xl',
    4: 'text-xl',
    5: 'text-lg',
    6: 'text-base',
  }
  return sizes[props.size]
})
</script>

<template>
  <div class="flex w-fit items-center" :class="headingSize">
    <div v-if="props.title" class="font-semibold inline-flex items-center">
      {{ props.title }}
    </div>
    <!-- Centred on the lowercase body of the title, which sits below its cap-height centre. -->
    <div v-if="$slots.default" class="ml-2 flex translate-y-[0.09em] items-center">
      <slot />
    </div>
    <TooltipProvider v-if="props.tooltip">
      <Tooltip :open="isTooltipOpen" @update:open="isTooltipOpen = $event">
        <TooltipTrigger as-child>
          <InputGroupButton
            class="rounded-full"
            size="icon-xs"
            @click="isTooltipOpen = !isTooltipOpen"
          >
            <InfoIcon class="size-3" />
          </InputGroupButton>
        </TooltipTrigger>
        <TooltipContent>{{ props.tooltip }}</TooltipContent>
      </Tooltip>
    </TooltipProvider>
  </div>
</template>
