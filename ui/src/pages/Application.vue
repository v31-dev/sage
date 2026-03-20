<script setup lang="ts">
import { useRoute } from 'vue-router'
import { Card, CardContent } from '@/components/ui/card'

const route = useRoute()
</script>

<template>
  <main class="flex-1 flex items-center justify-center px-4 py-8">
    <Card class="w-full max-w-md">
      <CardContent class="flex items-center justify-center py-16">
        <h1 class="text-3xl font-bold">{{ route.params.appId }}</h1>
      </CardContent>
    </Card>
  </main>
</template>
