<script setup lang="ts">
import { useRoute } from 'vue-router'
import LogViewer from '@/components/LogViewer.vue'

const route = useRoute()
</script>

<template>
  <LogViewer
    :project="route.params.projectId as string"
    :application="route.params.appId as string"
  />
</template>
