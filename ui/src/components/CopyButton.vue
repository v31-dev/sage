<script setup lang="ts">
import { useClipboard } from '@vueuse/core'
import { Check, Copy } from 'lucide-vue-next'
import { Button } from '@/components/ui/button'

const props = defineProps<{ text: string }>()

// Shows a check for two seconds after copying. Needs a secure context (https), which Sage
// is always served over; without clipboard support the button is left out.
const { copy, copied, isSupported } = useClipboard({ copiedDuring: 2000 })
</script>

<template>
  <Button
    v-if="isSupported"
    type="button"
    variant="ghost"
    size="icon-sm"
    :aria-label="copied ? 'Copied' : 'Copy to clipboard'"
    @click="copy(props.text)"
  >
    <Check v-if="copied" />
    <Copy v-else />
  </Button>
</template>
