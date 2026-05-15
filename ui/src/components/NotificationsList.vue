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
import { formatDateStringAgo } from '@/lib/utils'
import TitleStatus from './TitleStatus.vue'

const notifications = ref<Notification[]>([])
const isLoading = ref(true)
const isLoadingMore = ref(false)
const pageSize = 20
const pollInterval = 10_000
const pollTimer = ref<number | null>(null)

const unreadNotifications = computed(() => {
  if (notifications.value.length === 0) return false

  const lastSeenTimestamp = getLastSeenNotificationTimestamp()
  if (!lastSeenTimestamp) return true

  return new Date(notifications.value[0]!.created_at + 'Z') > new Date(lastSeenTimestamp)
})

const typeConfig = {
  info: { icon: 'radix-icons:info-circled', color: 'bg-blue-100 dark:bg-blue-900' },
  success: { icon: 'radix-icons:check-circled', color: 'bg-green-100 dark:bg-green-900' },
  warning: { icon: 'radix-icons:exclamation-triangle', color: 'bg-yellow-100 dark:bg-yellow-900' },
  error: { icon: 'radix-icons:cross-circled', color: 'bg-red-100 dark:bg-red-900' },
}

function getLastSeenNotificationTimestamp(): string | null {
  return localStorage.getItem('lastSeenNotificationTimestamp')
}

function setLastSeenNotificationTimestamp(): void {
  localStorage.setItem('lastSeenNotificationTimestamp', new Date().toISOString())
  notifications.value = [...notifications.value]
}

async function loadNotifications() {
  try {
    notifications.value = await fetchNotifications({ limit: pageSize })
  } catch (error) {
    console.error('Error loading notifications:', error)
  } finally {
    isLoading.value = false
  }
}

async function loadMore() {
  if (notifications.value.length === 0) {
    return
  }

  const lastNotification = notifications.value[notifications.value.length - 1]
  const to_ts = lastNotification ? lastNotification.created_at : null

  isLoadingMore.value = true
  try {
    const newNotifications = await fetchNotifications({ limit: pageSize, to_ts })
    if (newNotifications.length > 0) {
      const existingIds = new Set(notifications.value.map(notification => notification.id))
      notifications.value.push(
        ...newNotifications.filter(notification => !existingIds.has(notification.id))
      )
    }
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

onMounted(() => {
  loadNotifications()
  pollTimer.value = window.setInterval(async () => {
    // Get the date of the latest loaded notification (first in the list)
    const firstNotification = notifications.value[0]
    const from_ts = firstNotification ? firstNotification.created_at : null

    try {
      const newNotifications = await fetchNotifications({ limit: pageSize, from_ts })
      if (newNotifications.length > 0) {
        notifications.value.unshift(...newNotifications)
      }
    } catch (error) {
      console.error('Error loading more notifications:', error)
    }
  }, pollInterval)
})

onUnmounted(() => {
  if (pollTimer.value) {
    clearInterval(pollTimer.value)
  }
})
</script>

<template>
  <Popover>
    <PopoverTrigger as-child>
      <Button
        variant="ghost"
        size="icon"
        class="relative"
        @click="setLastSeenNotificationTimestamp"
      >
        <Icon icon="radix-icons:bell" class="h-[1rem] w-[1rem]" />
        <span
          v-if="unreadNotifications"
          class="absolute top-1 right-0 w-3 h-3 bg-red-500 text-white text-xs rounded-full flex items-center justify-center font-bold"
        >
        </span>
      </Button>
    </PopoverTrigger>
    <PopoverContent class="w-[calc(100vw-2rem)] sm:w-80 md:w-2xl max-h-96 overflow-y-auto">
      <div class="space-y-2 no-scrollbar">
        <TitleStatus title="Notifications" :size="4" />
        <Spinner v-if="isLoading" class="animate-spin" />

        <div v-else-if="notifications.length === 0" class="text-center text-sm text-gray-500">
          No notifications
        </div>

        <template v-else>
          <Item
            v-for="notification in notifications"
            :key="notification.id"
            variant="outline"
            :class="[notification.link && 'cursor-pointer hover:bg-gray-50 dark:hover:bg-gray-800']"
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
