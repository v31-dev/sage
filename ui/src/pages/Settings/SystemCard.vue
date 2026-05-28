<script setup lang="ts">
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { TriangleAlert } from 'lucide-vue-next'

import RestartSubCard from '@/pages/Settings/RestartSubCard.vue'
import TraefikResyncSubCard from '@/pages/Settings/TraefikResyncSubCard.vue'
</script>

<template>
  <Card class="overflow-hidden border-destructive/40">
    <CardHeader>
      <CardTitle class="flex items-center gap-2 text-destructive">
        <TriangleAlert class="h-4 w-4" />
        Danger Zone
      </CardTitle>
      <CardDescription> Destructive actions that affect the manager itself. </CardDescription>
    </CardHeader>

    <CardContent class="space-y-4">
      <TraefikResyncSubCard />
      <RestartSubCard />
    </CardContent>
  </Card>
</template>
