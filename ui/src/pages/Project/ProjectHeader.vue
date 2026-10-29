<script setup lang="ts">
import { Card, CardContent, CardHeader, CardTitle, CardAction } from '@/components/ui/card'
import { ButtonGroup } from '@/components/ui/button-group'
import { Label } from '@/components/ui/label'

import { type Project } from '@/services/api'
import EditProjectButton from './EditProjectButton.vue'
import DeleteProjectButton from './DeleteProjectButton.vue'
import SectionTitle from '@/components/SectionTitle.vue'

interface Props {
  project: Project
  loadProject: () => Promise<void>
}

const props = withDefaults(defineProps<Props>(), {})
</script>

<template>
  <Card>
    <CardHeader class="border-b">
      <CardTitle><SectionTitle :title="project.label" :size="3" /></CardTitle>
      <CardAction>
        <ButtonGroup class="space-x-1">
          <EditProjectButton :project="project" :loadProject="loadProject" />
          <DeleteProjectButton :project="project" />
        </ButtonGroup>
      </CardAction>
    </CardHeader>
    <CardContent>
      <div>
        <Label>Description</Label>
        <p class="text-sm text-muted-foreground">
          {{ project.description ? project.description : '-' }}
        </p>
      </div>
    </CardContent>
  </Card>
</template>
