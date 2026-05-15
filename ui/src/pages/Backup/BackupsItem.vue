<script setup lang="ts">
import { ref } from 'vue'
import {
  Item,
  ItemMedia,
  ItemContent,
  ItemTitle,
  ItemDescription,
  ItemActions,
} from '@/components/ui/item'
import { ArchiveRestore } from 'lucide-vue-next'
import { toast } from 'vue-sonner'

import { formatDate } from '@/lib/utils'
import ConfirmationButton from '@/components/ConfirmationButton.vue'
import { type Backup, backupAPI as backupAPIClass } from '@/services/api'
import RestoreLogsDialog from '@/components/RestoreLogsDialog.vue'

interface Props {
  backup: Backup
  backupAPI: typeof backupAPIClass
  loadBackups: () => Promise<void>
}

const props = withDefaults(defineProps<Props>(), {})

const restoreDialogTaskId = ref<string>('')

async function onConfirmDeleteBackup(backupId: string) {
  try {
    await props.backupAPI.delete(backupId)
    toast.success('Backup deleted successfully')
    await props.loadBackups()
  } catch (err) {
    toast.error(err instanceof Error ? err.message : 'Failed to delete backup')
  }
}

async function onConfirmRestoreBackup(backupId: string) {
  try {
    await props.backupAPI.action(`${backupId}/restore`)
    toast.success('Backup restore initiated successfully')
    await props.loadBackups()
  } catch (err) {
    // Extract task_id from error response headers and open logs dialog
    const taskId = (err as any)?.response?.headers?.get('X-Task-Id') || ''
    if (taskId) {
      restoreDialogTaskId.value = taskId
    }
    toast.error(err instanceof Error ? err.message : 'Failed to restore backup')
  }
}
</script>

<template>
  <Item variant="outline">
    <ItemMedia>
      <div
        class="size-8 rounded-full flex items-center justify-center bg-blue-100 dark:bg-blue-900"
      >
        <ArchiveRestore class="size-4 text-blue-600 dark:text-blue-300" />
      </div>
    </ItemMedia>
    <ItemContent>
      <ItemTitle>{{ formatDate(backup.created_at) }}</ItemTitle>
      <ItemDescription>{{ backup.s3_path }}</ItemDescription>
    </ItemContent>
    <ItemActions class="gap-2">
      <ConfirmationButton
        :title="'restore backup ' + formatDate(backup.created_at)"
        mode="info"
        description="Restore Platform Backup"
        buttonText="Restore"
        :icon="ArchiveRestore"
        :onConfirm="() => onConfirmRestoreBackup(String(backup.id))"
      />
      <ConfirmationButton
        :title="'Backup ' + formatDate(backup.created_at)"
        mode="delete"
        description="Platform Backup"
        :onConfirm="() => onConfirmDeleteBackup(String(backup.id))"
      />
    </ItemActions>
  </Item>
  <RestoreLogsDialog
    :taskId="restoreDialogTaskId"
    :objectTitle="'platform database ' + formatDate(backup.created_at)"
  />
</template>
