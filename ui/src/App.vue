<script setup lang="ts">
import { ref, onMounted, watchEffect } from 'vue'
import { useColorMode } from '@vueuse/core'
import Header from './components/layout/Header.vue'
import Footer from '@/components/layout/Footer.vue'
import PausedBanner from '@/components/layout/PausedBanner.vue'
import RestoreOverlay from '@/components/layout/RestoreOverlay.vue'
import AppSidebar from '@/components/layout/AppSidebar.vue'
import { SidebarInset, SidebarProvider } from '@/components/ui/sidebar'
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog'
import { Spinner } from '@/components/ui/spinner'
import 'vue-sonner/style.css'
import { Toaster } from '@/components/ui/sonner'
import { useAppStore } from '@/stores/app'

// Initialize theme immediately on page load
useColorMode()

const appStore = useAppStore()

watchEffect(() => {
  document.title = appStore.info?.name ?? 'sage'
})

const isLoading = ref(true)
const showError = ref(false)
const error = ref<string | null>(null)

const handleReload = () => {
  location.reload()
}

onMounted(async () => {
  try {
    await appStore.init()
    await new Promise(resolve => setTimeout(resolve, 500))
    isLoading.value = false
  } catch (err) {
    error.value = err instanceof Error ? err.message : 'Failed to load application'
    showError.value = true
    isLoading.value = false
  }
})
</script>

<template>
  <div>
    <!-- Loading Spinner -->
    <div
      v-if="isLoading"
      class="fixed inset-0 bg-background/80 backdrop-blur-sm z-50 flex items-center justify-center"
    >
      <div class="flex flex-col items-center gap-4">
        <Spinner />
        <p class="text-sm text-muted-foreground">Loading application...</p>
      </div>
    </div>

    <!-- Error Dialog -->
    <AlertDialog :open="showError">
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>Error Loading Application</AlertDialogTitle>
          <AlertDialogDescription>
            {{ error }}
          </AlertDialogDescription>
        </AlertDialogHeader>
        <div class="flex justify-end">
          <AlertDialogAction @click="handleReload">Reload</AlertDialogAction>
        </div>
      </AlertDialogContent>
    </AlertDialog>

    <!-- Main App -->
    <div v-if="!isLoading && !showError">
      <SidebarProvider>
        <AppSidebar />
        <SidebarInset class="h-screen overflow-hidden flex flex-col">
          <Header />
          <PausedBanner />
          <router-view class="flex-1 overflow-auto" />
          <Footer />
        </SidebarInset>
      </SidebarProvider>
    </div>

    <!-- Blocks the app while a platform restore is in progress -->
    <RestoreOverlay v-if="!isLoading && !showError" />

    <!-- Toast Notifications -->
    <Toaster />
  </div>
</template>
