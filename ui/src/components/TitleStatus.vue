<script setup lang="ts">
import { computed, ref } from 'vue'
import { type LucideIcon } from 'lucide-vue-next'
import Button from '@/components/ui/button/Button.vue'
import { Spinner } from '@/components/ui/spinner'
import { Tooltip, TooltipContent, TooltipTrigger, TooltipProvider } from '@/components/ui/tooltip'
import { InputGroupButton } from '@/components/ui/input-group'
import { InfoIcon } from 'lucide-vue-next'

interface Props {
  title?: string
  loading?: boolean
  status?: 'success' | 'warning' | 'error' | 'default'
  statusText?: string
  icon?: LucideIcon
  tooltip?: string
  size?: 1 | 2 | 3 | 4 | 5 | 6
}

const props = withDefaults(defineProps<Props>(), {
  title: '',
  loading: false,
  status: 'default',
  statusText: '',
  tooltip: '',
  size: 6,
})

const isTooltipOpen = ref(false)

const statusVariant = computed(() => {
  if (props.status === 'error') return 'destructive'
  else return 'ghost'
})

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
  <div class="flex w-fit items-center">
    <div
      v-if="props.title"
      class="font-semibold inline-flex items-center"
      :class="[headingSize, props.statusText == '' ? 'pr-0' : 'pr-2']"
    >
      {{ props.title }}
    </div>
    <Button
      v-if="props.statusText !== '' || props.loading === true || props.icon !== undefined"
      :variant="statusVariant"
      disabled
      size="sm"
      class="uppercase"
      :class="props.status"
    >
      <Spinner v-if="props.loading" class="animate-spin" />
      <component :is="props.icon" v-if="props.icon" />
      {{ props.statusText }}
    </Button>
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
