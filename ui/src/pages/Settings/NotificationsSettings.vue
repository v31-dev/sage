<script setup lang="ts">
import SettingsSection from './SettingsSection.vue'

const fields = [
  {
    key: 'discord_webhook',
    label: 'Discord Webhook',
    description: 'Saving sends a test message; leave empty to turn this channel off.',
    secret: true,
    placeholder: 'e.g. https://discord.com/api/webhooks/...',
  },
]
</script>

<template>
  <SettingsSection
    title="Notifications"
    description="Messages for operational events: deploys, backups, failures and the daily summary. Optional; every channel you set receives them."
    section="notifications"
    test-label="Send test message"
    :fields="fields"
  />
</template>
