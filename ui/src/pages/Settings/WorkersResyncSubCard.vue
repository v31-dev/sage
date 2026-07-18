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
import { settingsAPI } from '@/services/api'

const submitting = ref(false)
const dialogOpen = ref(false)
const errorMessage = ref('')

function openDialog() {
  errorMessage.value = ''
  dialogOpen.value = true
}

async function onConfirmResync() {
  submitting.value = true
  errorMessage.value = ''

  try {
    await settingsAPI.action('resync_workers')
    dialogOpen.value = false
  } catch (err) {
    errorMessage.value = err instanceof Error ? err.message : 'Failed to queue worker resync'
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <Card class="border-destructive/30 bg-destructive/5">
    <CardContent>
      <FieldSet>
        <FieldGroup>
          <Field>
            <FieldLabel>Resync Workers</FieldLabel>
            <FieldDescription>
              ⚠ Re-pushes compose, Traefik, and Vector templates to every online worker and restarts
              their base stack. Use when worker templates have drifted. Also re-pushes worker
              Traefik config, but does not clear acme.json or re-issue certificates — use Resync
              Traefik for that.
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
            Resync
          </Button>
        </DialogTrigger>

        <DialogContent
          class="sm:max-w-[480px]"
          :show-close-button="!submitting"
          @pointer-down-outside="submitting && $event.preventDefault()"
          @escape-key-down="submitting && $event.preventDefault()"
        >
          <DialogHeader>
            <DialogTitle class="flex items-center gap-2">
              <TriangleAlert class="h-4 w-4 text-destructive" />
              Resync Workers
            </DialogTitle>
            <DialogDescription>
              This queues a re-sync of every online worker that will run on the next scheduler tick
              (within ~30s). Use when worker compose, env, or Vector templates have drifted. Does
              not touch certificates — use Resync Traefik for cert/ACME issues.
            </DialogDescription>
          </DialogHeader>

          <div class="space-y-3 py-2 text-sm text-muted-foreground">
            <p>The following will happen for each online worker:</p>
            <ul class="ml-4 list-disc space-y-1 text-xs">
              <li>Compose, Traefik, Vector, and worker.env files are re-rsynced.</li>
              <li>The Cloudflare <code>*.int</code> DNS record is reasserted.</li>
              <li>
                <code>docker compose up -d</code> is run on the worker; base-stack services with
                config drift will recreate.
              </li>
              <li>
                Every application on the worker is flagged for Traefik domain config resync on the
                next scheduler tick.
              </li>
            </ul>
            <p
              class="rounded border border-destructive/40 bg-destructive/5 px-3 py-2 text-xs text-destructive"
            >
              <strong>Brief downtime expected.</strong> Worker Traefik may restart, causing a few
              seconds of dropped traffic to apps on that worker.
            </p>
          </div>

          <p v-if="errorMessage" class="text-xs text-destructive">{{ errorMessage }}</p>

          <DialogFooter>
            <Button variant="outline" size="sm" :disabled="submitting" @click="dialogOpen = false">
              Cancel
            </Button>
            <Button variant="destructive" size="sm" :disabled="submitting" @click="onConfirmResync">
              <Spinner v-if="submitting" class="animate-spin" />
              <RotateCw v-else class="h-3.5 w-3.5" />
              {{ submitting ? 'Queueing…' : 'Resync' }}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </CardFooter>
  </Card>
</template>
