<script setup lang="ts">
import { useRoute } from 'vue-router';
import SystemMetric from '@/components/SystemMetric.vue';

const route = useRoute();
const hostname = route.params.hostname as string;
</script>

<template>
  <SystemMetric :hostname="hostname" />
</template>
