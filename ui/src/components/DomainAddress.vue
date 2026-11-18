<script setup lang="ts">
import { ExternalLink } from 'lucide-vue-next'
import CopyButton from '@/components/CopyButton.vue'
import type { Domain } from '@/services/api'

// A domain's address with a copy button: a link for HTTP, text for TCP. It wraps instead of truncating, so a TCP
// address keeps its port in view.
const props = defineProps<{ domain: Pick<Domain, 'type' | 'address' | 'name'> }>()
</script>

<template>
  <div class="flex min-w-0 items-center gap-1">
    <a
      v-if="props.domain.type !== 'tcp' && props.domain.address"
      :href="props.domain.address"
      target="_blank"
      rel="noreferrer"
      class="flex min-w-0 items-center gap-1.5 text-sm underline-offset-4 hover:underline"
      data-address
    >
      <ExternalLink class="size-4 shrink-0" />
      <span class="break-all">{{ props.domain.address }}</span>
    </a>
    <span v-else class="min-w-0 break-all font-mono text-sm" data-address>
      {{ props.domain.address ?? props.domain.name }}
    </span>
    <CopyButton v-if="props.domain.address" :text="props.domain.address" />
  </div>
</template>
