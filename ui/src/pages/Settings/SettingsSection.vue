<script setup lang="ts">
import { computed } from 'vue'
import { OctagonX, Undo2 } from 'lucide-vue-next'
import { Alert, AlertDescription } from '@/components/ui/alert'
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog'
import { Button } from '@/components/ui/button'
import {
  Card,
  CardAction,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from '@/components/ui/card'
import { FieldGroup, FieldSet } from '@/components/ui/field'
import { Spinner } from '@/components/ui/spinner'
import SettingStateIndicator from '@/components/SettingStateIndicator.vue'
import TaskLogsButton from '@/pages/Tasks/TaskLogsButton.vue'
import type { SettingSection } from '@/services/api'
import { useSettingsStatusStore } from '@/stores/settingsStatus'

const props = withDefaults(
  defineProps<{
    title: string
    description: string
    section: SettingSection
    // Configured by env vars: shows status and values only, no form buttons.
    readonly?: boolean
    loading?: boolean
    saving?: boolean
    testing?: boolean
    dirty?: boolean
    error?: string
    leaveOpen?: boolean
    testLabel?: string
  }>(),
  {
    readonly: false,
    loading: false,
    saving: false,
    testing: false,
    dirty: false,
    error: '',
    leaveOpen: false,
    testLabel: 'Test',
  }
)

defineEmits<{ save: []; test: []; reset: []; leave: [leave: boolean] }>()

const statusStore = useSettingsStatusStore()
const status = computed(() => statusStore.status?.[props.section])
</script>

<template>
  <main class="flex-1 px-2 sm:px-4 py-4">
    <div class="max-w-3xl mx-auto flex flex-col gap-6">
      <Card>
        <CardHeader>
          <CardTitle>{{ title }}</CardTitle>
          <CardDescription>{{ description }}</CardDescription>
          <CardAction v-if="status">
            <SettingStateIndicator :state="status.state" show-label />
          </CardAction>
        </CardHeader>

        <CardContent class="space-y-6">
          <p
            v-if="status?.message || status?.task_id"
            class="flex flex-wrap items-center gap-2 text-sm text-muted-foreground"
          >
            <span v-if="status.message">{{ status.message }}</span>
            <TaskLogsButton v-if="status.task_id" :task-id="status.task_id" />
          </p>

          <div v-if="loading" class="flex items-center gap-3 py-8 text-sm text-muted-foreground">
            <Spinner class="animate-spin" />
            Loading settings...
          </div>
          <FieldSet v-else :disabled="saving">
            <FieldGroup>
              <slot />
            </FieldGroup>
          </FieldSet>

          <Alert v-if="error" variant="destructive">
            <OctagonX />
            <AlertDescription>{{ error }}</AlertDescription>
          </Alert>
        </CardContent>

        <CardFooter v-if="!readonly" class="flex flex-wrap gap-2">
          <slot name="save">
            <Button :disabled="saving || loading" @click="$emit('save')">
              <Spinner v-if="saving" class="animate-spin" />
              Save changes
            </Button>
          </slot>
          <Button variant="outline" :disabled="testing || loading" @click="$emit('test')">
            <Spinner v-if="testing" class="animate-spin" />
            {{ testLabel }}
          </Button>
          <Button v-if="dirty" variant="ghost" :disabled="saving" @click="$emit('reset')">
            <Undo2 />
            Reset
          </Button>
        </CardFooter>
      </Card>

      <slot name="after" />
    </div>

    <AlertDialog v-if="!readonly" :open="leaveOpen">
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>Discard unsaved changes?</AlertDialogTitle>
          <AlertDialogDescription>
            Your changes to the {{ title }} settings haven't been saved.
          </AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel @click="$emit('leave', false)">Stay</AlertDialogCancel>
          <AlertDialogAction @click="$emit('leave', true)">Discard changes</AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  </main>
</template>
