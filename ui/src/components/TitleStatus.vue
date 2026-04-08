<script setup lang="ts">
import { computed } from 'vue';
import { type LucideIcon } from 'lucide-vue-next';
import Button from '@/components/ui/button/Button.vue';
import { Spinner } from '@/components/ui/spinner';

interface Props {
  title?: string;
  loading?: boolean;
  status?: 'success' | 'warning' | 'error' | 'default';
  statusText?: string;
  icon?: LucideIcon;
}

const props = withDefaults(defineProps<Props>(), {
  title: '',
  loading: false,
  status: 'default',
  statusText: '',
});

const statusVariant = computed(() => {
  if (props.status === 'error') return 'destructive';
  else return 'ghost';
});
</script>

<template>
  <div class="flex w-fit items-stretch">
    <div
      v-if="props.title"
      class="uppercase text-sm inline-flex items-center pl-1"
      :class="props.statusText == '' ? 'pr-0' : 'pr-2'"
    >
      {{ props.title }}
    </div>
    <Button :variant="statusVariant" disabled size="sm" class="uppercase" :class="props.status">
      <Spinner v-if="props.loading" class="animate-spin" />
      <component :is="props.icon" v-if="props.icon" />
      {{ props.statusText }}
    </Button>
  </div>
</template>
