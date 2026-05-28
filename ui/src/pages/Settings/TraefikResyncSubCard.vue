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
    await settingsAPI.action('resync_traefik')
    dialogOpen.value = false
  } catch (err) {
    errorMessage.value = err instanceof Error ? err.message : 'Failed to initiate Traefik resync'
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
            <FieldLabel>Resync Traefik</FieldLabel>
            <FieldDescription>
              ⚠ Rewrites all Traefik config on the manager and online workers, restarts Traefik
              containers, and forces certificate re-issuance.
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
              Resync Traefik
            </DialogTitle>
            <DialogDescription>
              This rebuilds Traefik state across the platform. Use only when something is broken.
            </DialogDescription>
          </DialogHeader>

          <div class="space-y-3 py-2 text-sm text-muted-foreground">
            <p>The following will happen:</p>
            <ul class="ml-4 list-disc space-y-1 text-xs">
              <li>Manager Traefik config files and DNS API token file are rewritten.</li>
              <li>Manager Traefik is restarted; existing certificates are re-issued via ACME.</li>
              <li>
                Worker Traefik static and dynamic config is resynced; worker Traefik restarted.
              </li>
              <li>
                Every application is flagged for domain config resync on the next scheduler tick.
              </li>
            </ul>
            <p
              class="rounded border border-destructive/40 bg-destructive/5 px-3 py-2 text-xs text-destructive"
            >
              <strong>Brief downtime expected.</strong> Public traffic to all apps may drop for a
              few seconds during Traefik restarts.
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
              {{ submitting ? 'Triggering…' : 'Resync' }}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </CardFooter>
  </Card>
</template>
