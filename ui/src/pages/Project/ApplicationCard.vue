<script setup lang="ts">
import { useRouter } from 'vue-router'
import { Badge } from '@/components/ui/badge'
import { Card, CardHeader, CardTitle, CardContent, CardAction } from '@/components/ui/card'

import { type Application, type Project } from '@/services/api'
import SectionTitle from '@/components/SectionTitle.vue'
import StateIndicator from '@/components/StateIndicator.vue'
import { useAppStore } from '@/stores/app'

const router = useRouter()

interface Props {
  project: Project
  application: Application
}

const appStore = useAppStore()

const props = withDefaults(defineProps<Props>(), {})

function goToApplication(application: Application) {
  router.push(`/projects/${props.project.name}/${application.name}`)
}
</script>

<template>
  <Card
    class="cursor-pointer hover:shadow-lg transition-shadow flex flex-col"
    @click="goToApplication(props.application)"
  >
    <CardHeader>
      <CardTitle class="line-clamp-2">
        <SectionTitle :title="props.application.label">
          <StateIndicator
            v-bind="appStore.applicationIndicator(props.application.status)"
            show-label
          />
        </SectionTitle>
      </CardTitle>
      <CardAction>
        <Badge class="h-5 min-w-5 rounded-full px-1 font-mono tabular-nums">
          {{ props.application.container_count }}
        </Badge>
      </CardAction>
    </CardHeader>
    <CardContent class="overflow-hidden">
      <h3 class="text-sm font-medium text-muted-foreground mb-2">
        {{ props.application.description }}
      </h3>
    </CardContent>
  </Card>
</template>
