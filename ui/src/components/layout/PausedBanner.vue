<script setup lang="ts">
import { computed, ref } from 'vue'
import { PauseCircle } from 'lucide-vue-next'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { Spinner } from '@/components/ui/spinner'
import { resumeSage } from '@/services/api'
import { useAppStore } from '@/stores/app'

const appStore = useAppStore()
const state = computed(() => appStore.info?.state ?? 'running')
const resuming = ref(false)

async function onResume() {
  resuming.value = true
  try {
    await resumeSage()
    appStore.setState('running')
  } catch (err) {
    console.error('Failed to resume:', err)
  } finally {
    resuming.value = false
  }
}
</script>

<template>
  <Alert
    v-if="state === 'paused' || state === 'stopping'"
    variant="destructive"
    class="rounded-none border-x-0 border-t-0"
  >
    <PauseCircle />
    <AlertTitle>{{ state === 'paused' ? 'Sage is paused' : 'Sage is stopping' }}</AlertTitle>
    <AlertDescription class="flex flex-wrap items-center gap-3">
      <template v-if="state === 'paused'">
        Queued tasks wait and changes are refused until you resume.
        <RouterLink to="/upgrade" class="underline">Details</RouterLink>
        <Button size="sm" :disabled="resuming" @click="onResume">
          <Spinner v-if="resuming" class="animate-spin" />
          Resume
        </Button>
      </template>
      <template v-else>Running tasks are finishing; changes are refused.</template>
    </AlertDescription>
  </Alert>
</template>
