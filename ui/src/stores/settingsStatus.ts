import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import { fetchSettingsStatus, type SettingsStatus } from '@/services/api'

// Polled faster while a section is applying, so its state settles soon after.
const APPLYING_POLL_MS = 3000
const IDLE_POLL_MS = 30000

export const useSettingsStatusStore = defineStore('settingsStatus', () => {
  const status = ref<SettingsStatus | null>(null)
  const applying = computed(() =>
    Object.values(status.value ?? {}).some(section => section.state === 'applying')
  )
  let timer: ReturnType<typeof setTimeout> | null = null
  let started = false

  async function refresh() {
    if (timer) clearTimeout(timer)
    try {
      status.value = await fetchSettingsStatus()
    } catch (err) {
      console.error('Failed to load settings status:', err)
    }
    timer = setTimeout(refresh, applying.value ? APPLYING_POLL_MS : IDLE_POLL_MS)
  }

  function start() {
    if (started) return
    started = true
    refresh()
    window.addEventListener('focus', refresh)
  }

  return { status, refresh, start }
})
