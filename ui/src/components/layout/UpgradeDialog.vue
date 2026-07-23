<script setup lang="ts">
import { computed, ref } from 'vue'
import MarkdownIt from 'markdown-it'
import DOMPurify from 'dompurify'
import { Icon } from '@iconify/vue'
import { RefreshCw, TriangleAlert } from 'lucide-vue-next'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import {
  Card,
  CardAction,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card'
import {
  Dialog,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogScrollContent,
  DialogTitle,
  DialogTrigger,
} from '@/components/ui/dialog'
import { Skeleton } from '@/components/ui/skeleton'
import { Spinner } from '@/components/ui/spinner'
import { formatDate } from '@/lib/utils'
import {
  fetchAppInfo,
  fetchRelease,
  refreshRelease,
  settingsAPI,
  type Release,
} from '@/services/api'
import { useAppStore } from '@/stores/app'

type Phase = 'idle' | 'upgrading' | 'waiting'

const appStore = useAppStore()

const updateAvailable = computed(() => {
  const info = appStore.info
  return !!(info?.latest_version && info.latest_version !== info.version)
})

const dialogOpen = ref(false)
const phase = ref<Phase>('idle')
const release = ref<Release | null>(null)
const notesLoading = ref(false)
const notesError = ref('')
const errorMessage = ref('')
const refreshing = ref(false)

const md = new MarkdownIt({ linkify: true, breaks: false })
md.renderer.rules.link_open = (tokens, idx, options, _env, self) => {
  const token = tokens[idx]
  token?.attrSet('target', '_blank')
  token?.attrSet('rel', 'noreferrer')
  return self.renderToken(tokens, idx, options)
}

const renderedNotes = computed(() => {
  const body = release.value?.body
  if (!body) return ''
  return DOMPurify.sanitize(md.render(body), { ADD_ATTR: ['target'] })
})

const publishedDate = computed(() =>
  release.value?.published_at ? formatDate(release.value.published_at) : ''
)

async function onRefresh() {
  refreshing.value = true
  errorMessage.value = ''
  const before = release.value?.fetched_at ?? null
  try {
    await refreshRelease()
    const deadline = Date.now() + 60_000
    let updated = false
    while (Date.now() < deadline) {
      await sleep(5000)
      const latest = await fetchRelease()
      if (latest.fetched_at && latest.fetched_at !== before) {
        release.value = latest
        await appStore.init()
        updated = true
        break
      }
    }
    if (!updated) errorMessage.value = 'Refresh timed out. Please try again.'
  } catch (err) {
    errorMessage.value = err instanceof Error ? err.message : 'Failed to refresh release'
  } finally {
    refreshing.value = false
  }
}

async function onOpen(open: boolean) {
  dialogOpen.value = open
  if (!open) return
  errorMessage.value = ''
  if (!release.value) await loadNotes()
}

async function loadNotes() {
  notesLoading.value = true
  notesError.value = ''
  try {
    release.value = await fetchRelease()
  } catch (err) {
    notesError.value = err instanceof Error ? err.message : 'Failed to load release notes'
  } finally {
    notesLoading.value = false
  }
}

async function onConfirmUpgrade() {
  phase.value = 'upgrading'
  errorMessage.value = ''
  let startTimeBefore = ''

  try {
    startTimeBefore = (await fetchAppInfo()).start_time
    await settingsAPI.action('upgrade')
  } catch (err) {
    errorMessage.value = err instanceof Error ? err.message : 'Failed to initiate upgrade'
    phase.value = 'idle'
    return
  }

  phase.value = 'waiting'
  await pollUntilBack(startTimeBefore)
}

async function pollUntilBack(startTimeBefore: string) {
  // The manager is replaced only after the updater pulls the new image, so this
  // can take a few minutes; poll until a fresh process answers, then reload.
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
  <Dialog v-if="appStore.info" v-model:open="dialogOpen" @update:open="onOpen">
    <DialogTrigger as-child>
      <button type="button" class="inline-flex cursor-pointer items-center">
        <Badge v-if="updateAvailable">
          <Icon icon="mdi:arrow-up-bold-circle-outline" />
          v{{ appStore.info?.latest_version }}
        </Badge>
        <span v-else class="text-xs text-muted-foreground transition-colors hover:text-foreground">
          v{{ appStore.info?.version }}
        </span>
      </button>
    </DialogTrigger>

    <DialogScrollContent
      class="sm:max-w-[560px]"
      :show-close-button="phase === 'idle'"
      @pointer-down-outside="phase !== 'idle' && $event.preventDefault()"
      @escape-key-down="phase !== 'idle' && $event.preventDefault()"
    >
      <DialogHeader>
        <DialogTitle class="flex items-center gap-2">
          <Icon
            :icon="
              updateAvailable ? 'mdi:arrow-up-bold-circle-outline' : 'mdi:check-circle-outline'
            "
            class="h-4 w-4"
          />
          {{ updateAvailable ? 'Update available' : 'Sage is up to date' }}
        </DialogTitle>
        <DialogDescription>
          <template v-if="updateAvailable">
            v{{ appStore.info?.version }} → v{{ appStore.info?.latest_version }}
          </template>
          <template v-else>
            You're on the latest version (v{{ appStore.info?.version }}).
          </template>
        </DialogDescription>
      </DialogHeader>

      <Card v-if="notesLoading">
        <CardContent class="space-y-2">
          <Skeleton class="h-4 w-1/3" />
          <Skeleton class="h-4 w-full" />
          <Skeleton class="h-4 w-5/6" />
          <Skeleton class="h-4 w-2/3" />
        </CardContent>
      </Card>
      <Alert v-else-if="notesError" variant="destructive">
        <TriangleAlert />
        <AlertTitle>Couldn't load release notes</AlertTitle>
        <AlertDescription>{{ notesError }}</AlertDescription>
      </Alert>
      <Card v-else-if="release">
        <CardHeader>
          <CardTitle>
            <Button
              v-if="release.html_url"
              as="a"
              :href="release.html_url"
              target="_blank"
              rel="noreferrer"
              variant="link"
              class="h-auto p-0 text-base font-semibold"
            >
              {{ release.name || `v${release.version}` }}
            </Button>
            <template v-else>{{ release.name || `v${release.version}` }}</template>
          </CardTitle>
          <CardDescription v-if="publishedDate">{{ publishedDate }}</CardDescription>
          <CardAction>
            <Button
              variant="ghost"
              size="icon-sm"
              title="Check for updates"
              :disabled="phase !== 'idle' || refreshing"
              @click="onRefresh"
            >
              <RefreshCw :class="['h-3.5 w-3.5', refreshing && 'animate-spin']" />
            </Button>
          </CardAction>
        </CardHeader>
        <CardContent>
          <div class="release-notes text-sm text-muted-foreground" v-html="renderedNotes" />
        </CardContent>
      </Card>

      <Alert v-if="updateAvailable">
        <TriangleAlert />
        <AlertTitle>What happens on upgrade</AlertTitle>
        <AlertDescription>
          <ul class="list-disc space-y-1">
            <li>
              In-flight operations (deploys, backups, syncs) finish first, then all queued tasks are
              cancelled.
            </li>
            <li>
              A database backup is taken, then the new image is pulled and the manager restarts.
            </li>
            <li>
              If the new version fails its health check it is automatically rolled back to v{{
                appStore.info?.version
              }}.
            </li>
            <li>
              This page will wait and reload once the manager is back — it can take a few minutes
              while the image downloads.
            </li>
          </ul>
        </AlertDescription>
      </Alert>

      <Alert v-if="errorMessage" variant="destructive">
        <TriangleAlert />
        <AlertDescription>{{ errorMessage }}</AlertDescription>
      </Alert>

      <DialogFooter>
        <Button
          variant="outline"
          size="sm"
          :disabled="phase !== 'idle'"
          @click="dialogOpen = false"
        >
          Close
        </Button>
        <Button
          v-if="updateAvailable"
          size="sm"
          :disabled="phase !== 'idle' || notesLoading || refreshing"
          @click="onConfirmUpgrade"
        >
          <Spinner v-if="phase !== 'idle'" class="animate-spin" />
          <Icon v-else icon="mdi:arrow-up-bold-circle-outline" class="h-3.5 w-3.5" />
          {{ phase === 'waiting' ? 'Waiting…' : phase === 'upgrading' ? 'Starting…' : 'Upgrade' }}
        </Button>
      </DialogFooter>
    </DialogScrollContent>
  </Dialog>
</template>

<style scoped>
.release-notes :deep(h1),
.release-notes :deep(h2),
.release-notes :deep(h3) {
  margin: 0.75rem 0 0.35rem;
  font-weight: 600;
  color: var(--foreground);
}
.release-notes :deep(h1) {
  font-size: 1rem;
}
.release-notes :deep(h2) {
  font-size: 0.95rem;
}
.release-notes :deep(h3) {
  font-size: 0.9rem;
}
.release-notes :deep(ul),
.release-notes :deep(ol) {
  margin: 0.35rem 0;
  padding-left: 1.25rem;
  list-style: disc;
}
.release-notes :deep(ol) {
  list-style: decimal;
}
.release-notes :deep(li) {
  margin: 0.15rem 0;
}
.release-notes :deep(p) {
  margin: 0.4rem 0;
}
.release-notes :deep(a) {
  color: var(--primary);
  text-decoration: underline;
  text-underline-offset: 2px;
}
.release-notes :deep(code) {
  padding: 0.1rem 0.3rem;
  border-radius: 0.25rem;
  background: var(--muted);
  font-size: 0.85em;
}
.release-notes :deep(pre) {
  margin: 0.5rem 0;
  padding: 0.6rem;
  border-radius: 0.375rem;
  background: var(--muted);
  overflow-x: auto;
}
.release-notes :deep(pre code) {
  padding: 0;
  background: transparent;
}
.release-notes :deep(hr) {
  margin: 0.75rem 0;
  border-color: var(--border);
}
</style>
