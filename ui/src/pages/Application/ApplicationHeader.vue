<script setup lang="ts">
import { computed } from 'vue';
import router from '@/router';
import { toast } from 'vue-sonner';
import { Logs, Activity } from 'lucide-vue-next';
import {
  Card,
  CardAction,
  CardContent,
  CardHeader,
  CardTitle,
  CardFooter,
} from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { ButtonGroup } from '@/components/ui/button-group';
import { Label } from '@/components/ui/label';

import DeployApplicationButton from './DeployApplicationButton.vue';
import StopApplicationButton from './StopApplicationButton.vue';
import EditApplicationButton from './EditApplicationButton.vue';
import DeleteConfirmationButton from '@/components/DeleteConfirmationButton.vue';
import { type Application, getApplicationAPI } from '@/services/api';
import TitleStatus from '@/components/TitleStatus.vue';

interface Props {
  application: Application;
  applicationAPI: ReturnType<typeof getApplicationAPI>;
  loadApplication: () => Promise<void>;
}

const props = withDefaults(defineProps<Props>(), {});

function onClickLogs() {
  router.push(`/projects/${props.application.project.name}/${props.application.name}/logs`);
}

function onClickMetrics() {
  router.push(`/projects/${props.application.project.name}/${props.application.name}/metrics`);
}

async function onConfirmDelete() {
  await props.applicationAPI.delete(props.application.name);
  toast.success(`Application ${props.application.name} deleted successfully`);
  router.push(`/projects/${props.application.project.name}`);
}

const applicationStatusClass = computed(() => {
  if (props.application.status === 'active') return 'success';
  else if (['deploying', 'stopping'].includes(props.application.status)) return 'warning';
  else return 'default';
});
</script>

<template>
  <Card>
    <CardHeader class="border-b">
      <CardTitle class="uppercase gap-2 flex items-center">
        <TitleStatus
          :title="props.application.label"
          :status="applicationStatusClass"
          :loading="['deploying', 'stopping'].includes(props.application.status)"
          :statusText="props.application.status"
        />
      </CardTitle>
      <CardAction>
        <ButtonGroup class="space-x-1">
          <EditApplicationButton
            :application="props.application"
            :applicationAPI="props.applicationAPI"
            :loadApplication="props.loadApplication"
          />
          <DeleteConfirmationButton
            title="Application"
            :description="props.application.name"
            :onConfirm="onConfirmDelete"
          />
        </ButtonGroup>
      </CardAction>
    </CardHeader>
    <CardContent class="space-y-4">
      <div>
        <Label>Description</Label>
        <p class="text-sm text-muted-foreground">
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
        <Label>Repository</Label>
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
    </CardContent>
    <CardFooter
      class="border-t flex flex-col md:flex-row justify-between items-center gap-2 md:gap-0"
    >
      <ButtonGroup class="space-x-1 w-full md:w-auto flex">
        <DeployApplicationButton
          :application="props.application"
          :applicationAPI="props.applicationAPI"
        />
        <StopApplicationButton
          :application="props.application"
          :applicationAPI="props.applicationAPI"
        />
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
