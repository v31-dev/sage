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
import { useAppStore } from '@/stores/app'

const appStore = useAppStore()
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
    appStore.setSettingApplying('cloudflare')
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
              ⚠ Re-checks the manager's and the workers' certificates (a missing, invalid or
              soon-to-expire one is issued again), pushes the workers' certificate and Traefik
              config to every online worker, restores their *.int DNS records and resyncs every
              application's routing. Use when a certificate is broken or routing has drifted on the
              workers.
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
              This re-checks the certificates and resyncs Traefik on every online worker. Use it for
              certificate problems or routing drift, not for general worker drift.
            </DialogDescription>
          </DialogHeader>

          <div class="space-y-3 py-2 text-sm text-muted-foreground">
            <p>The following will happen:</p>
            <ul class="ml-4 list-disc space-y-1 text-xs">
              <li>
                The manager's and the workers' certificates are checked; one that is missing,
                invalid or within 30 days of expiry is issued again through ACME. The manager serves
                a new certificate without a restart.
              </li>
              <li>
                The workers' certificate and Traefik config are pushed to every online worker;
                Traefik reloads them from its watched files.
              </li>
              <li>Each online worker's *.int DNS record is created again in Cloudflare.</li>
              <li>Every application's routing is resynced.</li>
            </ul>
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
