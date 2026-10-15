<script setup lang="ts">
import CustomInput from '@/components/CustomInput.vue'
import SettingField from './SettingField.vue'
import SettingsSection from './SettingsSection.vue'
import { useSettingForm } from './useSettingForm'

const form = useSettingForm('notifications', ['discord_webhook'], 'Notifications')
const { fields, isLoading, saving, testing, error, dirty, leaveOpen } = form
</script>

<template>
  <SettingsSection
    title="Notifications"
    description="Discord messages for operational events: deploys, backups, failures and the daily summary. Optional."
    section="notifications"
    test-label="Send test message"
    :loading="isLoading"
    :saving="saving"
    :testing="testing"
    :dirty="dirty"
    :error="error"
    :leave-open="leaveOpen"
    @save="form.save"
    @test="form.test"
    @reset="form.reset"
    @leave="form.resolveLeave"
  >
    <SettingField
      id="notifications-discord-webhook"
      label="Discord Webhook"
      description="Saving sends a test message; leave empty to turn notifications off."
    >
      <CustomInput
        id="notifications-discord-webhook"
        secret
        :model-value="fields.discord_webhook"
        @update:model-value="value => form.updateField('discord_webhook', value)"
        placeholder="e.g. https://discord.com/api/webhooks/..."
      />
    </SettingField>
  </SettingsSection>
</template>
