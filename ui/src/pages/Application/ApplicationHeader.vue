<script setup lang="ts">
import { computed } from 'vue'
import { useRouter } from 'vue-router'
import { toast } from 'vue-sonner'
import { Logs, Activity, TriangleAlert, Trash } from 'lucide-vue-next'
import {
  Card,
  CardAction,
  CardContent,
  CardHeader,
  CardTitle,
  CardFooter,
} from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { ButtonGroup } from '@/components/ui/button-group'
import { Label } from '@/components/ui/label'
import { Icon } from '@iconify/vue'

import DeployApplicationButton from './DeployApplicationButton.vue'
import StopApplicationButton from './StopApplicationButton.vue'
import EditApplicationButton from './EditApplicationButton.vue'
import ConfirmationButton from '@/components/ConfirmationButton.vue'
import { type Application, deleteApplication } from '@/services/api'
import SectionTitle from '@/components/SectionTitle.vue'
import StateIndicator from '@/components/StateIndicator.vue'
import { useAppStore } from '@/stores/app'
import { formatDateStringAgo } from '@/lib/utils'
import { TONES } from '@/lib/tone'

interface Props {
  application: Application
  loadApplication: () => Promise<void>
}

const appStore = useAppStore()
const router = useRouter()

const props = withDefaults(defineProps<Props>(), {})

function onClickLogs() {
  router.push(`/projects/${props.application.project.name}/${props.application.name}/logs`)
}

function onClickMetrics() {
  router.push(`/projects/${props.application.project.name}/${props.application.name}/metrics`)
}

async function onConfirmDelete() {
  await deleteApplication(props.application.project.name, props.application.name)
  toast.success(`Application ${props.application.name} deleted successfully`)
  router.push(`/projects/${props.application.project.name}`)
}

const repoURL = computed(() => {
  const repo = props.application.repo
  if (!repo) return ''

  let [base, gitref] = repo.split('#')
  base = base?.replace(/\.git$/, '') // Remove .git suffix if present

  // No branch/path specified
  if (!gitref) return base

  const [branch, path] = gitref.split(':')

  // Branch only
  if (!path) return `${base}/tree/${branch}`

  // Branch + path
  return `${base}/tree/${branch}/${path}`
})
</script>

<template>
  <Card>
    <CardHeader class="border-b">
      <CardTitle>
        <SectionTitle :title="props.application.label" :size="3">
          <StateIndicator
            v-bind="appStore.applicationIndicator(appStore.applicationDeployStatus)"
            show-label
          />
        </SectionTitle>
      </CardTitle>
      <CardAction>
        <ButtonGroup class="space-x-1">
          <EditApplicationButton
            :application="props.application"
            :loadApplication="props.loadApplication"
          />
          <ConfirmationButton
            triggerText="Delete"
            title="Delete Application"
            body="Are you sure you want to delete this application? This action cannot be undone."
            :description="props.application.name"
            :icon="Trash"
            destructive
            :onConfirm="onConfirmDelete"
          />
        </ButtonGroup>
      </CardAction>
    </CardHeader>
    <CardContent class="space-y-4">
      <div>
        <Label>Description</Label>
        <p class="text-sm text-muted-foreground whitespace-pre-wrap">
          {{ props.application.description ? props.application.description : '-' }}
        </p>
      </div>
      <div>
        <Label>Type</Label>
        <p class="text-sm text-muted-foreground">
          {{ props.application.type ? props.application.type : '-' }}
        </p>
      </div>
      <div v-if="props.application.type === 'docker'">
        <Label>Image</Label>
        <p class="text-sm text-muted-foreground">
          {{ props.application.image ? props.application.image : '-' }}
        </p>
      </div>
      <div v-if="props.application.type === 'git'">
        <div class="flex items-center gap-2">
          <Label>Repository</Label>
          <a
            v-if="props.application.repo"
            :href="repoURL"
            target="_blank"
            class="text-muted-foreground hover:text-foreground transition-colors"
          >
            <Icon icon="mdi:github" class="w-4 h-4" />
          </a>
        </div>
        <p class="text-sm text-muted-foreground">
          {{ props.application.repo ? props.application.repo : '-' }}
        </p>
      </div>
      <div v-if="props.application.type === 'git'">
        <Label>Path</Label>
        <p class="text-sm text-muted-foreground">
          {{ props.application.path ? props.application.path : '-' }}
        </p>
      </div>
      <div v-if="props.application.command">
        <Label>Command</Label>
        <p class="text-sm text-muted-foreground whitespace-pre-wrap">
          {{ props.application.command }}
        </p>
      </div>
      <div>
        <Label>Last Deployed</Label>
        <p class="text-sm text-muted-foreground">
          {{
            props.application.deployed_at ? formatDateStringAgo(props.application.deployed_at) : '-'
          }}
        </p>
      </div>
      <div
        v-if="props.application.config_dirty"
        :class="['flex items-center gap-1.5 text-sm', TONES.warning.text]"
      >
        <TriangleAlert class="w-4 h-4 shrink-0" />
        Configuration changed since last deploy — redeploy to apply.
      </div>
    </CardContent>
    <CardFooter
      class="border-t flex flex-col md:flex-row justify-between items-center gap-2 md:gap-0"
    >
      <ButtonGroup class="space-x-1 w-full md:w-auto flex">
        <DeployApplicationButton :application="props.application" />
        <StopApplicationButton :application="props.application" />
      </ButtonGroup>
      <ButtonGroup class="space-x-1 w-full md:w-auto flex">
        <Button size="sm" class="flex-1 md:flex-initial" variant="outline" @click="onClickLogs">
          <Logs />Logs
        </Button>
        <Button size="sm" class="flex-1 md:flex-initial" variant="outline" @click="onClickMetrics">
          <Activity />Metrics
        </Button>
      </ButtonGroup>
    </CardFooter>
  </Card>
</template>
