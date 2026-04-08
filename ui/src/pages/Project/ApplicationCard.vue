<script setup lang="ts">
import { computed } from 'vue'
import { useRouter } from 'vue-router'
import { Badge } from '@/components/ui/badge'
import { Card, CardHeader, CardTitle, CardContent, CardAction } from '@/components/ui/card'
import { Button } from '@/components/ui/button';

import { type Application, type Project } from '@/services/api';
import TitleStatus from '@/components/TitleStatus.vue'


const router = useRouter()

interface Props {
  project: Project
  application: Application
}

const props = withDefaults(defineProps<Props>(), {})

function goToApplication(application: Application) {
  router.push(`/projects/${props.project.name}/${application.name}`)
}

const applicationStatusClass = computed(() => {
  if (props.application.status === 'active')
    return 'success'
  else if (['deploying', 'stopping'].includes(props.application.status))
    return 'warning'
  else
    return 'default'
})
</script>

<template>
  <Card class="cursor-pointer hover:shadow-lg transition-shadow flex flex-col"
    @click="goToApplication(props.application)">
    <CardHeader>
      <CardTitle class="line-clamp-2">
        <TitleStatus
          :title="props.application.label"
          :status="applicationStatusClass"
          :loading="['deploying', 'stopping'].includes(props.application.status)"
          :statusText="props.application.status"
        />
      </CardTitle>
      <CardAction>
        <Badge class="h-5 min-w-5 rounded-full px-1 font-mono tabular-nums">
          {{ props.application.container_count }}
        </Badge>
      </CardAction>
    </CardHeader>
    <CardContent class="overflow-hidden">
      <h3 class="text-sm font-medium text-muted-foreground mb-2">{{ props.application.description }}</h3>
    </CardContent>
  </Card>
</template>