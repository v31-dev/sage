<script setup lang="ts">
import { computed } from 'vue'
import CustomInput from '@/components/CustomInput.vue'
import { useSettingsStatusStore } from '@/stores/settingsStatus'
import SettingField from './SettingField.vue'
import SettingsSection from './SettingsSection.vue'

const fieldKeys = ['discord_webhook']

// Why each channel that failed its last check failed.
const statusStore = useSettingsStatusStore()
const failures = computed(() => statusStore.status?.notifications.channels ?? {})
</script>

<template>
  <SettingsSection
    title="Notifications"
    description="Messages for operational events: deploys, backups, failures and the daily summary. Optional; every channel you set receives them."
    section="notifications"
    test-label="Send test message"
    :field-keys="fieldKeys"
  >
    <template #default="{ fields, updateField }">
      <SettingField
        id="notifications-discord-webhook"
        label="Discord Webhook"
        description="Saving sends a test message; leave empty to turn this channel off."
        :error="failures.discord_webhook"
      >
        <CustomInput
          id="notifications-discord-webhook"
          secret
          :model-value="fields.discord_webhook"
          @update:model-value="value => updateField('discord_webhook', value)"
          placeholder="e.g. https://discord.com/api/webhooks/..."
        />
      </SettingField>
    </template>
  </SettingsSection>
</template>
