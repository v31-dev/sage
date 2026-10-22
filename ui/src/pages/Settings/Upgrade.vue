<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import MarkdownIt from 'markdown-it'
import DOMPurify from 'dompurify'
import { Icon } from '@iconify/vue'
import { CircleCheck, RefreshCw, TriangleAlert } from 'lucide-vue-next'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import {
  Card,
  CardAction,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card'
import { Skeleton } from '@/components/ui/skeleton'
import { Spinner } from '@/components/ui/spinner'
import { formatDate } from '@/lib/utils'
import {
  fetchRelease,
  refreshRelease,
  settingsAPI,
  tasksAPI,
  type Release,
  type TasksResponse,
} from '@/services/api'
import { useAppStore } from '@/stores/app'
import { TONES } from '@/lib/tone'

const appStore = useAppStore()

const updateAvailable = computed(() => {
  const info = appStore.info
  return !!(info?.latest_version && info.latest_version !== info.version)
})

const release = ref<Release | null>(null)
const notesLoading = ref(true)
const notesError = ref('')
const refreshing = ref(false)
const refreshError = ref('')

const md = new MarkdownIt({ html: true, linkify: true, breaks: false })
md.renderer.rules.link_open = (tokens, idx, options, _env, self) => {
  const token = tokens[idx]
  token?.attrSet('target', '_blank')
  token?.attrSet('rel', 'noreferrer')
  return self.renderToken(tokens, idx, options)
}

// GitHub alerts: a quote that starts with [!NOTE], [!TIP], [!IMPORTANT], [!WARNING] or [!CAUTION].
const ALERT_MARKER = /^\[!(NOTE|TIP|IMPORTANT|WARNING|CAUTION)\]\s*/i
md.core.ruler.after('inline', 'github-alerts', state => {
  const tokens = state.tokens
  for (let i = 0; i + 2 < tokens.length; i++) {
    if (tokens[i]?.type !== 'blockquote_open' || tokens[i + 1]?.type !== 'paragraph_open') continue
    const children = tokens[i + 2]?.children
    const first = children?.[0]
    const match = first?.type === 'text' ? ALERT_MARKER.exec(first.content) : null
    if (!children || !first || !match) continue
    const kind = match[1]!.toLowerCase()
    first.content = first.content.slice(match[0].length)
    if (!first.content) children.splice(0, children[1]?.type === 'softbreak' ? 2 : 1)
    tokens[i]!.attrJoin('class', `markdown-alert markdown-alert-${kind}`)
    const title = new state.Token('html_block', '', 0)
    title.content = `<p class="markdown-alert-title">${kind[0]!.toUpperCase()}${kind.slice(1)}</p>\n`
    tokens.splice(i + 1, 0, title)
  }
})

const renderedNotes = computed(() => {
  const body = release.value?.body
  if (!body) return ''
  return DOMPurify.sanitize(md.render(body), { ADD_ATTR: ['target'] })
})

const publishedDate = computed(() =>
  release.value?.published_at ? formatDate(release.value.published_at) : ''
)

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

async function onRefresh() {
  refreshing.value = true
  refreshError.value = ''
  const before = release.value?.fetched_at ?? null
  try {
    await refreshRelease()
    const deadline = Date.now() + 60_000
    while (Date.now() < deadline) {
      await new Promise(resolve => setTimeout(resolve, 5000))
      const latest = await fetchRelease()
      if (latest.fetched_at && latest.fetched_at !== before) {
        release.value = latest
        await appStore.refresh()
        return
      }
    }
    refreshError.value = 'Refresh timed out. Please try again.'
  } catch (err) {
    refreshError.value = err instanceof Error ? err.message : 'Failed to refresh release'
  } finally {
    refreshing.value = false
  }
}

// Pause / resume ("prepare to stop")
const tasks = ref<TasksResponse | null>(null)
const toggling = ref(false)
const pauseError = ref('')
let tasksTimer: ReturnType<typeof setInterval> | null = null

const state = computed(() => appStore.info?.state ?? 'running')
const running = computed(() => tasks.value?.running ?? [])
const queuedCount = computed(() => tasks.value?.queued.length ?? 0)
const safeToStop = computed(() => state.value === 'paused' && running.value.length === 0)

async function loadTasks() {
  try {
    tasks.value = (await tasksAPI.fetchAll()) as unknown as TasksResponse
  } catch (err) {
    console.error('Failed to load tasks:', err)
  }
}

async function onToggle() {
  toggling.value = true
  pauseError.value = ''
  try {
    const next = state.value === 'paused' ? 'running' : 'paused'
    await settingsAPI.action(next === 'running' ? 'resume' : 'pause')
    appStore.setState(next)
    await loadTasks()
  } catch (err) {
    pauseError.value = err instanceof Error ? err.message : 'Failed to change state'
  } finally {
    toggling.value = false
  }
}

onMounted(() => {
  loadNotes()
  loadTasks()
  tasksTimer = setInterval(loadTasks, 2000)
})

onUnmounted(() => {
  if (tasksTimer) clearInterval(tasksTimer)
})
</script>

<template>
  <main class="flex-1 px-2 sm:px-4 py-4 sm:py-4 relative">
    <div class="max-w-7xl mx-auto flex flex-col gap-6">
      <Card>
        <CardHeader>
          <CardTitle class="flex items-center gap-2">
            <Icon
              :icon="
                updateAvailable ? 'mdi:arrow-up-bold-circle-outline' : 'mdi:check-circle-outline'
              "
              class="h-4 w-4"
            />
            {{ updateAvailable ? 'Update available' : 'Sage is up to date' }}
          </CardTitle>
          <CardDescription>
            <template v-if="updateAvailable">
              v{{ appStore.info?.version }} → v{{ appStore.info?.latest_version }}
            </template>
            <template v-else
              >You're on the latest version (v{{ appStore.info?.version }}).</template
            >
          </CardDescription>
          <CardAction>
            <Button variant="outline" size="sm" :disabled="refreshing" @click="onRefresh">
              <RefreshCw :class="['h-3.5 w-3.5', refreshing && 'animate-spin']" />
              Check for updates
            </Button>
          </CardAction>
        </CardHeader>
        <CardContent class="space-y-4">
          <Alert v-if="refreshError" variant="destructive">
            <TriangleAlert />
            <AlertDescription>{{ refreshError }}</AlertDescription>
          </Alert>

          <div v-if="notesLoading" class="space-y-2">
            <Skeleton class="h-4 w-1/3" />
            <Skeleton class="h-4 w-full" />
            <Skeleton class="h-4 w-5/6" />
          </div>
          <Alert v-else-if="notesError" variant="destructive">
            <TriangleAlert />
            <AlertTitle>Couldn't load release notes</AlertTitle>
            <AlertDescription>{{ notesError }}</AlertDescription>
          </Alert>
          <div v-else-if="release" class="space-y-1">
            <div class="flex items-baseline gap-2">
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
              <span v-else class="text-base font-semibold">
                {{ release.name || `v${release.version}` }}
              </span>
              <span v-if="publishedDate" class="text-sm text-muted-foreground">
                {{ publishedDate }}
              </span>
            </div>
            <div class="release-notes text-sm text-muted-foreground" v-html="renderedNotes" />
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>How to upgrade</CardTitle>
          <CardDescription>Sage doesn't upgrade itself; you replace its container.</CardDescription>
        </CardHeader>
        <CardContent>
          <ol class="list-decimal space-y-1 pl-5 text-sm">
            <li>
              Take a backup on the
              <RouterLink to="/backups" class="underline">Backups</RouterLink> page and wait for it
              to appear in the list.
            </li>
            <li>Pause Sage (below) and wait until it shows it's safe to stop.</li>
            <li>Stop the Sage container.</li>
            <li>
              Change its image tag to the new version:
              <code
                >ghcr.io/v31-dev/sage:{{ appStore.info?.latest_version || '&lt;version&gt;' }}</code
              >
            </li>
            <li>Start it again. Sage always starts unpaused.</li>
          </ol>
          <p class="mt-3 text-sm text-muted-foreground">
            Stopping without pausing is also safe: on a stop signal Sage lets running tasks finish
            before it exits, as long as the container's stop grace period gives it time.
          </p>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Prepare to stop</CardTitle>
          <CardDescription>
            While paused, queued tasks wait, running tasks finish, and changes are refused.
          </CardDescription>
          <CardAction>
            <Button
              :variant="state === 'paused' ? 'default' : 'outline'"
              size="sm"
              :disabled="toggling || state === 'stopping'"
              @click="onToggle"
            >
              <Spinner v-if="toggling" class="animate-spin" />
              {{ state === 'paused' ? 'Resume' : 'Pause' }}
            </Button>
          </CardAction>
        </CardHeader>
        <CardContent class="space-y-3 text-sm">
          <Alert v-if="pauseError" variant="destructive">
            <TriangleAlert />
            <AlertDescription>{{ pauseError }}</AlertDescription>
          </Alert>

          <p v-if="state === 'running'" class="text-muted-foreground">Sage is running normally.</p>
          <p v-else-if="state === 'stopping'" class="text-muted-foreground">Sage is stopping.</p>
          <template v-else>
            <p v-if="safeToStop" class="flex items-center gap-2 font-medium">
              <CircleCheck :class="['h-4 w-4', TONES.success.text]" />
              Safe to stop.
              <span v-if="queuedCount" class="font-normal text-muted-foreground">
                {{ queuedCount }} queued task(s) will be cancelled when Sage stops.
              </span>
            </p>
            <div v-else class="space-y-1">
              <p class="flex items-center gap-2">
                <Spinner class="animate-spin" />
                Waiting for {{ running.length }} running task(s) to finish:
              </p>
              <ul class="list-disc pl-5 text-muted-foreground">
                <li v-for="task in running" :key="task.task_id">{{ task.name }}</li>
              </ul>
            </div>
          </template>
        </CardContent>
      </Card>
    </div>
  </main>
</template>

<style scoped>
.release-notes :deep(h1),
.release-notes :deep(h2),
.release-notes :deep(h3),
.release-notes :deep(h4) {
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
.release-notes :deep(h4) {
  font-size: 0.85rem;
}
.release-notes :deep(details) {
  margin-top: 0.75rem;
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
.release-notes :deep(summary) {
  cursor: pointer;
  font-weight: 600;
  color: var(--foreground);
}
.release-notes :deep(summary h3) {
  display: inline;
  margin: 0;
}
.release-notes :deep(blockquote) {
  margin: 0.5rem 0;
  padding: 0.1rem 0.75rem;
  border-left: 3px solid var(--border);
}
.release-notes :deep(.markdown-alert) {
  --alert: var(--info);
  border-left-color: var(--alert);
  border-radius: 0 0.375rem 0.375rem 0;
  background: color-mix(in oklab, var(--alert) 10%, transparent);
}
.release-notes :deep(.markdown-alert-tip) {
  --alert: var(--success);
}
.release-notes :deep(.markdown-alert-important),
.release-notes :deep(.markdown-alert-warning) {
  --alert: var(--warning);
}
.release-notes :deep(.markdown-alert-caution) {
  --alert: var(--destructive);
}
.release-notes :deep(.markdown-alert-title) {
  margin: 0.3rem 0 0;
  font-weight: 600;
  color: var(--alert);
}
.release-notes :deep(hr) {
  margin: 0.75rem 0;
  border-color: var(--border);
}
</style>
