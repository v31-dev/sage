<script setup lang="ts">
import { ref } from 'vue'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardFooter } from '@/components/ui/card'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from '@/components/ui/dialog'
import { Field, FieldDescription, FieldGroup, FieldLabel, FieldSet } from '@/components/ui/field'
import { Spinner } from '@/components/ui/spinner'
import { RotateCw, TriangleAlert } from 'lucide-vue-next'
import { fetchAppInfo, restartManager } from '@/services/api'

type RestartPhase = 'idle' | 'restarting' | 'waiting'

const phase = ref<RestartPhase>('idle')
const dialogOpen = ref(false)
const errorMessage = ref('')

function openDialog() {
  errorMessage.value = ''
  dialogOpen.value = true
}

async function onConfirmRestart() {
  phase.value = 'restarting'
  errorMessage.value = ''
  let startTimeBefore: string = ''

  try {
    startTimeBefore = (await fetchAppInfo()).start_time
    await restartManager()
  } catch (err) {
    if (!(err instanceof TypeError)) {
      errorMessage.value = err instanceof Error ? err.message : 'Failed to initiate restart'
      phase.value = 'idle'
      return
    }
  }

  phase.value = 'waiting'
  await pollUntilRestarted(startTimeBefore)
}

async function pollUntilRestarted(startTimeBefore: string) {
  for (;;) {
    await sleep(2000)
    try {
      const startTimeAfter = (await fetchAppInfo()).start_time
      if (new Date(startTimeAfter) > new Date(startTimeBefore)) {
        window.location.reload()
        return
      }
    } catch {}
  }
}

function sleep(ms: number) {
  return new Promise(resolve => setTimeout(resolve, ms))
}
</script>

<template>
  <Card class="border-destructive/30 bg-destructive/5">
    <CardContent>
      <FieldSet>
        <FieldGroup>
          <Field>
            <FieldLabel>Restart Manager</FieldLabel>
            <FieldDescription>
              ⚠ If the restart fails you will need to intervene manually on the host.
            </FieldDescription>
          </Field>
        </FieldGroup>
      </FieldSet>
    </CardContent>
    <CardFooter>
      <Dialog v-model:open="dialogOpen">
        <DialogTrigger as-child>
          <Button variant="destructive" @click="openDialog">
            <RotateCw class="h-3.5 w-3.5" />
            Restart
          </Button>
        </DialogTrigger>

        <DialogContent
          class="sm:max-w-[480px]"
          :show-close-button="phase === 'idle'"
          @pointer-down-outside="phase !== 'idle' && $event.preventDefault()"
          @escape-key-down="phase !== 'idle' && $event.preventDefault()"
        >
          <DialogHeader>
            <DialogTitle class="flex items-center gap-2">
              <TriangleAlert class="h-4 w-4 text-destructive" />
              Restart Manager
            </DialogTitle>
            <DialogDescription>
              This will stop and restart the manager container immediately.
            </DialogDescription>
          </DialogHeader>

          <div class="space-y-3 py-2 text-sm text-muted-foreground">
            <p>The following will happen:</p>
            <ul class="ml-4 list-disc space-y-1 text-xs">
              <li>The manager container receives a restart signal.</li>
              <li>All in-flight operations (deploys, backups, syncs) will be interrupted.</li>
              <li>Docker will bring the container back up automatically.</li>
              <li>The UI will poll for the service and reload once it's back.</li>
            </ul>
            <p
              class="rounded border border-destructive/40 bg-destructive/5 px-3 py-2 text-xs text-destructive"
            >
              <strong>No progress is tracked.</strong> If the restart fails or the container does
              not come back on its own, you will need to intervene manually on the host (e.g.
              <code>docker compose up -d</code>).
            </p>
          </div>

          <p v-if="errorMessage" class="text-xs text-destructive">{{ errorMessage }}</p>

          <DialogFooter>
            <Button
              variant="outline"
              size="sm"
              :disabled="phase !== 'idle'"
              @click="dialogOpen = false"
            >
              Cancel
            </Button>
            <Button
              variant="destructive"
              size="sm"
              :disabled="phase !== 'idle'"
              @click="onConfirmRestart"
            >
              <Spinner v-if="phase !== 'idle'" class="animate-spin" />
              <RotateCw v-else class="h-3.5 w-3.5" />
              {{
                phase === 'waiting'
                  ? 'Waiting…'
                  : phase === 'restarting'
                    ? 'Restarting…'
                    : 'Restart'
              }}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </CardFooter>
  </Card>
</template>
