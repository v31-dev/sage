<script setup lang="ts">
import {
  Logs,
  Activity
} from 'lucide-vue-next'
import {
  Card,
  CardAction,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
  CardFooter
} from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { ButtonGroup } from '@/components/ui/button-group'
import { Label } from '@/components/ui/label'

import DeployApplicationButton from './DeployApplicationButton.vue'
import StopApplicationButton from './StopApplicationButton.vue'
import EditApplicationButton from './EditApplicationButton.vue'
import DeleteApplicationButton from './DeleteApplicationButton.vue'
import { 
  type Application,
  getApplicationAPI
} from '@/services/api'


interface Props {
  application: Application,
  applicationAPI: ReturnType<typeof getApplicationAPI>,
  loadApplication: () => Promise<void>
}

const props = withDefaults(defineProps<Props>(), {})
</script>

<template>
  <Card>
    <CardHeader class="border-b">
      <CardTitle class="text-2xl">{{ props.application.label }}</CardTitle>
      <CardDescription>{{ props.application.name }}</CardDescription>
      <CardAction>
        <ButtonGroup class="space-x-1">
          <EditApplicationButton :application="props.application" :applicationAPI="props.applicationAPI"
            :loadApplication="props.loadApplication" />
          <DeleteApplicationButton :application="props.application" :applicationAPI="props.applicationAPI" />
        </ButtonGroup>
      </CardAction>
    </CardHeader>
    <CardContent class="space-y-4">
      <div>
        <Label>Description</Label>
        <p class="text-sm text-muted-foreground">{{ props.application.description ? props.application.description : '-' }}</p>
      </div>
      <div>
        <Label>Type</Label>
        <p class="text-sm text-muted-foreground">{{ props.application.type ? props.application.type : '-' }}</p>
      </div>
      <div v-if="props.application.type === 'docker'">
        <Label>Image</Label>
        <p class="text-sm text-muted-foreground">{{ props.application.image ? props.application.image : '-' }}</p>
      </div>
      <div v-if="props.application.type === 'git'">
        <Label>Repository</Label>
        <p class="text-sm text-muted-foreground">{{ props.application.repo ? props.application.repo : '-' }}</p>
      </div>
      <div v-if="props.application.type === 'git'">
        <Label>Path</Label>
        <p class="text-sm text-muted-foreground">{{ props.application.path ? props.application.path : '-' }}</p>
      </div>
    </CardContent>
    <CardFooter class="border-t flex flex-col md:flex-row justify-between items-center gap-2 md:gap-0">
      <ButtonGroup class="space-x-1 w-full md:w-auto flex">
        <DeployApplicationButton :application="props.application" :applicationAPI="props.applicationAPI" />
        <StopApplicationButton :application="props.application" :applicationAPI="props.applicationAPI" />
      </ButtonGroup>
      <ButtonGroup class="space-x-1 w-full md:w-auto flex">
        <Button class="flex-1 md:flex-initial" variant="outline">
          <Logs />Logs
        </Button>
        <Button class="flex-1 md:flex-initial" variant="outline">
          <Activity />Metrics
        </Button>
      </ButtonGroup>
    </CardFooter>
  </Card>
</template>