<script setup lang="ts">
import {
  Card,
  CardAction,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card'
import { ButtonGroup } from '@/components/ui/button-group'

import { type ProjectPage } from '@/services/api'
import SectionTitle from '@/components/SectionTitle.vue'
import AddApplicationButton from './AddApplicationButton.vue'
import ApplicationCard from './ApplicationCard.vue'
import DeleteProjectButton from './DeleteProjectButton.vue'
import EditProjectButton from './EditProjectButton.vue'

interface Props {
  project: ProjectPage
  loadProjects: () => Promise<void>
}

const props = withDefaults(defineProps<Props>(), {})
</script>

<template>
  <Card>
    <CardHeader class="border-b">
      <CardTitle><SectionTitle :title="props.project.label" :size="3" /></CardTitle>
      <CardDescription v-if="props.project.description">
        {{ props.project.description }}
      </CardDescription>
      <CardAction>
        <ButtonGroup class="space-x-1">
          <AddApplicationButton :project="props.project" :loadProjects="props.loadProjects" />
          <EditProjectButton :project="props.project" :loadProjects="props.loadProjects" />
          <DeleteProjectButton :project="props.project" :loadProjects="props.loadProjects" />
        </ButtonGroup>
      </CardAction>
    </CardHeader>
    <CardContent>
      <p v-if="props.project.applications.length === 0" class="text-sm text-muted-foreground">
        No applications
      </p>
      <div v-else class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
        <ApplicationCard
          v-for="app in props.project.applications"
          :key="app.name"
          :application="app"
          :project="props.project"
        />
      </div>
    </CardContent>
  </Card>
</template>
