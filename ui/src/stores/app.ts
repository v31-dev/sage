import { ref } from 'vue'
import { defineStore } from 'pinia'

import { fetchAppInfo, type AppInfo, type Application } from '@/services/api'

export const useAppStore = defineStore('app', () => {
  const info = ref<AppInfo | null>(null)

  // Immediate updates to block UI button till polling gives the real update
  const applicationDeployStatus = ref('inactive' as Application['status'])

  async function init() {
    info.value = await fetchAppInfo()
  }

  function updateApplicationDeployStatus(status: Application['status']) {
    applicationDeployStatus.value = status
  }

  return {
    // State
    info,
    applicationDeployStatus,
    // Actions
    init,
    updateApplicationDeployStatus,
  }
})
