<script setup lang="ts">
import { ref, computed } from 'vue'
import { toast } from 'vue-sonner'
import { Edit, Trash, ExternalLink } from 'lucide-vue-next'
import {
  Card,
  CardHeader,
  CardTitle,
  CardAction,
  CardFooter
} from '@/components/ui/card'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import {
  Field,
  FieldGroup,
  FieldLabel,
  FieldSet,
  FieldError,
} from '@/components/ui/field'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { Spinner } from '@/components/ui/spinner'
import { ButtonGroup } from '@/components/ui/button-group'
import { useAppStore } from '@/stores/app'

import {
  type Domain,
  getDomainAPI,
} from '@/services/api'
import DeleteConfirmationButton from '@/components/DeleteConfirmationButton.vue'


interface Props {
  domain: Domain
  domainAPI: ReturnType<typeof getDomainAPI>
  loadApplication: () => Promise<void>
}

const props = withDefaults(defineProps<Props>(), {})
const appStore = useAppStore()

const isEditDomainDialogOpen = ref(false)
const editedDomainName = ref('')
const editedDomainType = ref<'internal' | 'public'>('internal')
const editDomainErrorMessage = ref('')
const isClickedEditDomainConfirm = ref(false)
const link = computed(() => {
  if (props.domain.type === 'public') {
    return `https://${props.domain.name}.${appStore.info!.domain}`
  } else {
    return `https://${props.domain.name}.int.${appStore.info!.domain}`
  }
})

function openEditDomainDialog() {
  editDomainErrorMessage.value = ''
  editedDomainName.value = props.domain.name
  editedDomainType.value = props.domain.type
  isEditDomainDialogOpen.value = true
}

async function onClickEditDomainConfirm() {
  editDomainErrorMessage.value = ''

  if (!editedDomainName.value.trim()) {
    editDomainErrorMessage.value = 'Please enter a domain name'
    return
  }

  try {
    isClickedEditDomainConfirm.value = true
    await props.domainAPI.update(props.domain.name, {
      name: editedDomainName.value.trim(),
      type: editedDomainType.value,
    })
    isEditDomainDialogOpen.value = false
    await props.loadApplication()
    toast.success('Domain updated successfully')
  } catch (err) {
    editDomainErrorMessage.value = err instanceof Error ? err.message : 'Failed to update domain'
  } finally {
    isClickedEditDomainConfirm.value = false
  }
}

async function onClickConfirmDelete() {
  await props.domainAPI.delete(props.domain.name)
  await props.loadApplication()
  toast.success('Domain deleted successfully')
}
</script>

<template>
  <Card>
    <CardHeader>
      <CardTitle>
        <Button size="sm" variant="ghost" class="uppercase" disabled>
          {{ props.domain.name }}
        </Button>
      </CardTitle>
      <CardAction>
        <ButtonGroup class="space-x-1">
          <Button size="sm" variant="outline" @click="openEditDomainDialog" class="gap-2">
            <Edit />
            Edit
          </Button>
          <DeleteConfirmationButton title="Domain" :description="props.domain.name" :onConfirm="onClickConfirmDelete" />
        </ButtonGroup>
      </CardAction>
    </CardHeader>
    <CardFooter class="border-t">
      <Button size="sm" variant="outline" class="w-full flex-1" as-child>
        <a :href="link" target="_blank">
          <ExternalLink />
          {{ link }}
        </a>
      </Button>
    </CardFooter>
  </Card>

  <!-- Edit Domain Dialog -->
  <Dialog v-model:open="isEditDomainDialogOpen">
    <DialogContent class="sm:max-w-[400px]">
      <DialogHeader>
        <DialogTitle>Edit Domain</DialogTitle>
        <DialogDescription>
          {{ props.domain.name }}
        </DialogDescription>
      </DialogHeader>
      <FieldSet>
        <FieldGroup>
          <Field>
            <FieldLabel for="edit-domain-name">
              Domain Name
            </FieldLabel>
            <Input id="edit-domain-name" v-model="editedDomainName" placeholder="e.g., example.com"
              @keyup.enter="onClickEditDomainConfirm" />
          </Field>
          <Field>
            <FieldLabel for="edit-domain-type">
              Type
            </FieldLabel>
            <Select v-model="editedDomainType">
              <SelectTrigger id="edit-domain-type">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="internal">
                  Internal
                </SelectItem>
                <SelectItem value="public">
                  Public
                </SelectItem>
              </SelectContent>
            </Select>
          </Field>
          <Field>
            <FieldError v-if="editDomainErrorMessage">{{ editDomainErrorMessage }}</FieldError>
          </Field>
        </FieldGroup>
      </FieldSet>
      <DialogFooter>
        <Button size="sm" type="button" variant="outline" @click="isEditDomainDialogOpen = false"
          :disabled="isClickedEditDomainConfirm">
          Cancel
        </Button>
        <Button size="sm" @click="onClickEditDomainConfirm" :disabled="isClickedEditDomainConfirm || !editedDomainName.trim()">
          <Spinner class="animate-spin" v-if="isClickedEditDomainConfirm" />
          Save
        </Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>