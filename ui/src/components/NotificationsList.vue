<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { Button } from '@/components/ui/button'
import {
  Item,
  ItemActions,
  ItemContent,
  ItemDescription,
  ItemMedia,
  ItemTitle,
} from '@/components/ui/item'
import { Spinner } from '@/components/ui/spinner'
import { Icon } from '@iconify/vue'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'

import { fetchNotifications, type Notification } from '@/services/api'
import { watchFeed } from '@/lib/updates'
import { formatDateStringAgo } from '@/lib/utils'
import SectionTitle from './SectionTitle.vue'
import { TONES } from '@/lib/tone'

const notifications = ref<Notification[]>([])
const isLoading = ref(true)
const isLoadingMore = ref(false)
const pageSize = 20
let stop: (() => void) | undefined

const LAST_SEEN_KEY = 'lastSeenNotificationId'
const lastSeenId = ref(Number(localStorage.getItem(LAST_SEEN_KEY)) || 0)

const unreadNotifications = computed(() => (notifications.value[0]?.id ?? 0) > lastSeenId.value)

const typeConfig = {
  info: { icon: 'radix-icons:info-circled', color: TONES.info.soft },
  success: { icon: 'radix-icons:check-circled', color: TONES.success.soft },
  warning: { icon: 'radix-icons:exclamation-triangle', color: TONES.warning.soft },
  error: { icon: 'radix-icons:cross-circled', color: TONES.error.soft },
}

function markSeen() {
  lastSeenId.value = notifications.value[0]?.id ?? lastSeenId.value
  localStorage.setItem(LAST_SEEN_KEY, String(lastSeenId.value))
}

function addNotifications(rows: Notification[]) {
  const known = new Set(notifications.value.map(notification => notification.id))
  const fresh = rows.filter(notification => !known.has(notification.id))
  if (fresh.length > 0) {
    notifications.value = [...notifications.value, ...fresh].sort((a, b) => b.id - a.id)
  }
}

// What the list does not have yet: the newest page at first, then whatever came after its newest.
function loadNewest() {
  const newest = notifications.value[0]
  return fetchNotifications(newest ? { from_ts: newest.created_at } : { limit: pageSize })
}

async function loadMore() {
  if (notifications.value.length === 0) {
    return
  }

  const lastNotification = notifications.value[notifications.value.length - 1]
  const to_ts = lastNotification ? lastNotification.created_at : null

  isLoadingMore.value = true
  try {
    addNotifications(await fetchNotifications({ limit: pageSize, to_ts }))
  } catch (error) {
    console.error('Error loading more notifications:', error)
  } finally {
    isLoadingMore.value = false
  }
}

function handleNotificationClick(notification: Notification) {
  if (notification.link) {
    window.open(notification.link, '_blank')
  }
}

onMounted(async () => {
  const watch = watchFeed('notifications', loadNewest, addNotifications)
  stop = watch.stop
  try {
    await watch.first
  } catch (error) {
    console.error('Error loading notifications:', error)
  } finally {
    isLoading.value = false
  }
})

onUnmounted(() => stop?.())
</script>

<template>
  <Popover>
    <PopoverTrigger as-child>
      <Button variant="ghost" size="icon" class="relative" @click="markSeen">
        <Icon icon="radix-icons:bell" class="h-[1rem] w-[1rem]" />
        <span
          v-if="unreadNotifications"
          role="img"
          aria-label="New notifications"
          :class="[
            'absolute top-1 right-0 size-2.5 text-white text-xs rounded-full flex items-center justify-center font-bold',
            TONES.error.dot,
          ]"
        >
        </span>
      </Button>
    </PopoverTrigger>
    <PopoverContent class="w-[calc(100vw-2rem)] sm:w-80 md:w-2xl max-h-96 overflow-y-auto">
      <div class="space-y-2 no-scrollbar">
        <SectionTitle title="Notifications" :size="4" />
        <Spinner v-if="isLoading" class="animate-spin" />

        <div
          v-else-if="notifications.length === 0"
          class="text-center text-sm text-muted-foreground"
        >
          No notifications
        </div>

        <template v-else>
          <Item
            v-for="notification in notifications"
            :key="notification.id"
            variant="outline"
            :class="[notification.link && 'cursor-pointer hover:bg-muted/50']"
            @click="notification.link && handleNotificationClick(notification)"
          >
            <ItemMedia>
              <div
                :class="[
                  'size-8 rounded-full flex items-center justify-center',
                  typeConfig[notification.type as keyof typeof typeConfig]?.color,
                ]"
              >
                <Icon
                  :icon="typeConfig[notification.type as keyof typeof typeConfig]?.icon"
                  class="size-5 text-current"
                />
              </div>
            </ItemMedia>
            <ItemContent>
              <ItemTitle>{{ notification.content }}</ItemTitle>
              <ItemDescription>{{ formatDateStringAgo(notification.created_at) }}</ItemDescription>
            </ItemContent>
            <ItemActions v-if="notification.link">
              <Icon icon="radix-icons:chevron-right" class="size-4" />
            </ItemActions>
          </Item>

          <div class="flex justify-center mt-4">
            <Button variant="outline" @click="loadMore" class="w-full" :disabled="isLoadingMore"
              ><Spinner v-if="isLoadingMore" class="animate-spin" /> Load More</Button
            >
          </div>
        </template>
      </div>
    </PopoverContent>
  </Popover>
</template>
