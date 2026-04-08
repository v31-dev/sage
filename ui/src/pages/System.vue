<script setup lang="ts">
import { useAppStore } from '@/stores/app';
import SystemMetric from '@/components/SystemMetric.vue';

const store = useAppStore();
</script>

<template>
  <SystemMetric v-if="store.info" :hostname="store.info.hostname" />
</template>
