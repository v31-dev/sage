<script setup lang="ts">
import { ref } from 'vue'
import { InfoIcon } from 'lucide-vue-next'
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '@/components/ui/tooltip'

defineProps<{ text: string }>()

// Opens on tap as well as on hover and focus, so it works on touch screens.
const open = ref(false)
</script>

<template>
  <TooltipProvider>
    <Tooltip :open="open" @update:open="open = $event">
      <TooltipTrigger as-child>
        <button
          type="button"
          aria-label="Help"
          class="text-muted-foreground hover:text-foreground focus-visible:ring-ring/50 rounded-full outline-none focus-visible:ring-[3px]"
          @click="open = !open"
        >
          <InfoIcon class="size-3.5" />
        </button>
      </TooltipTrigger>
      <TooltipContent>{{ text }}</TooltipContent>
    </Tooltip>
  </TooltipProvider>
</template>
