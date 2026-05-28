<script setup lang="ts">
import NotificationsSettingsCard from './NotificationsSettingsCard.vue'
import CloudflareSettingsCard from './CloudflareSettingsCard.vue'
import S3SettingsCard from './S3SettingsCard.vue'
import SystemCard from './SystemCard.vue'
</script>

<template>
  <main class="flex-1 px-2 sm:px-4 py-4 sm:py-4 relative">
    <div class="max-w-7xl mx-auto">
      <div class="flex flex-col gap-6">
        <CloudflareSettingsCard />
        <S3SettingsCard />
        <NotificationsSettingsCard />
        <SystemCard />
      </div>
    </div>
  </main>
</template>
