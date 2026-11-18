<script setup lang="ts">
import { type Component } from 'vue'
import { Button } from '@/components/ui/button'
import { Spinner } from '@/components/ui/spinner'
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '@/components/ui/tooltip'

// An action as its icon alone, named by `label`, which is also its tooltip. The tooltip hangs on a wrapper, so a
// disabled button still shows it; attributes, such as a wrapping trigger's click, go to the button itself.
defineOptions({ inheritAttrs: false })

const props = defineProps<{
  icon: Component
  label: string
  destructive?: boolean
  disabled?: boolean
  loading?: boolean
}>()
</script>

<template>
  <TooltipProvider>
    <Tooltip>
      <TooltipTrigger as-child>
        <span class="inline-flex" data-icon-button>
          <Button
            v-bind="$attrs"
            variant="ghost"
            size="icon-sm"
            :disabled="props.disabled"
            :aria-label="props.label"
            :class="props.destructive && 'text-destructive hover:text-destructive'"
          >
            <Spinner v-if="props.loading" class="animate-spin" />
            <component :is="props.icon" v-else />
          </Button>
        </span>
      </TooltipTrigger>
      <TooltipContent>{{ props.label }}</TooltipContent>
    </Tooltip>
  </TooltipProvider>
</template>
