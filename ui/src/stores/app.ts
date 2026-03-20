import { ref } from "vue";
import { defineStore } from "pinia";

import { fetchAppInfo, type AppInfo } from "@/services/api";


export const useAppStore = defineStore("app", () => {
  // State
  const info = ref<AppInfo | null>(null);

  // Actions
  async function init() {
    info.value = await fetchAppInfo();
  }

  return {
    // State
    info,
    // Actions
    init,
  };
});