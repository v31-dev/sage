<script setup lang="ts">
import { computed } from 'vue'
import { Input } from '@/components/ui/input'
import ConfirmationButton from '@/components/ConfirmationButton.vue'
import { useAppStore } from '@/stores/app'
import SettingField from './SettingField.vue'
import SettingsSection from './SettingsSection.vue'
import WorkersResyncSubCard from './WorkersResyncSubCard.vue'
import { useSettingForm } from './useSettingForm'

const appStore = useAppStore()
const form = useSettingForm('tailscale', ['worker_tag'], 'Tailscale')
const { fields, saved, isLoading, saving, testing, error, dirty, leaveOpen } = form

// Changing an existing tag changes which machines count as workers, so it is confirmed.
const isChangingWorkerTag = computed(
  () =>
    saved.value.worker_tag !== '' &&
    (fields.value.worker_tag ?? '').trim().replace(/^tag:/, '') !== saved.value.worker_tag
)

async function onConfirmSave() {
  await form.save()
  if (error.value) throw new Error(error.value)
}
</script>

<template>
  <SettingsSection
    title="Tailscale"
    :description="`Worker discovery: machines on your tailnet that carry the worker tag become workers. This manager is ${appStore.info?.name} (${appStore.info?.ip}).`"
    section="tailscale"
    test-label="Test tag"
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
      id="tailscale-worker-tag"
      label="Worker Tag"
      description="Machines with this tag become workers. With or without the tag: prefix; leave empty to turn worker discovery off."
    >
      <Input
        id="tailscale-worker-tag"
        :model-value="fields.worker_tag"
        @update:model-value="value => form.updateField('worker_tag', value)"
        placeholder="e.g. worker"
      />
    </SettingField>

    <template v-if="isChangingWorkerTag" #save>
      <ConfirmationButton
        trigger-text="Save changes"
        title="Change the worker tag?"
        body="Machines that don't carry the new tag stop counting as workers: they are marked offline, their containers are taken out of routing, and deploys or stops on them fail until the tag matches again."
        confirm-text="Change tag"
        :disabled="saving || isLoading"
        :loading="saving"
        :on-confirm="onConfirmSave"
      />
    </template>

    <template #after>
      <WorkersResyncSubCard />
    </template>
  </SettingsSection>
</template>
