<script setup lang="ts">
import { computed } from 'vue'
import { Badge } from '@/components/ui/badge'
import { SETTING_STATES } from '@/lib/settingState'
import type { SettingState } from '@/services/api'

const props = defineProps<{ state: SettingState; showLabel?: boolean }>()

const meta = computed(() => SETTING_STATES[props.state])
</script>

<template>
  <Badge v-if="showLabel" variant="outline" :class="meta.text">
    <component :is="meta.icon" :class="state === 'applying' && 'animate-spin'" />
    {{ meta.label }}
  </Badge>
  <component
    v-else
    :is="meta.icon"
    role="img"
    :aria-label="meta.label"
    :class="['size-4 shrink-0', meta.text, state === 'applying' && 'animate-spin']"
  />
</template>
