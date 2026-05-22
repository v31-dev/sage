<script setup lang="ts">
import { ref, computed } from 'vue'
import { toast } from 'vue-sonner'
import { Edit, ExternalLink, CircleCheck } from 'lucide-vue-next'
import { Card, CardHeader, CardTitle, CardAction, CardFooter } from '@/components/ui/card'
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
  InputGroup,
  InputGroupAddon,
  InputGroupInput,
  InputGroupText,
} from '@/components/ui/input-group'
import { Field, FieldGroup, FieldLabel, FieldSet, FieldError } from '@/components/ui/field'
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

import { type Domain, getDomainAPI, type Application } from '@/services/api'
import TitleStatus from '@/components/TitleStatus.vue'
import ConfirmationButton from '@/components/ConfirmationButton.vue'

interface Props {
  application: Application
  domain: Domain
  domainAPI: ReturnType<typeof getDomainAPI>
  loadApplication: () => Promise<void>
}

const props = withDefaults(defineProps<Props>(), {})
const appStore = useAppStore()

const isEditDomainDialogOpen = ref(false)
const domain = ref({
  name: '',
  type: 'internal' as 'internal' | 'public',
  port: 80,
})
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
  domain.value.name = props.domain.name
  domain.value.type = props.domain.type
  domain.value.port = props.domain.port
  isEditDomainDialogOpen.value = true
}

async function onClickEditDomainConfirm() {
  editDomainErrorMessage.value = ''

  if (!domain.value.name.trim()) {
    editDomainErrorMessage.value = 'Please enter a domain name'
    return
  }

  try {
    isClickedEditDomainConfirm.value = true
    await props.domainAPI.update(`${props.domain.type}:${props.domain.name}`, domain.value)
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
  await props.domainAPI.delete(`${props.domain.type}:${props.domain.name}`)
  await props.loadApplication()
  toast.success('Domain deleted successfully')
}
</script>

<template>
  <Card>
    <CardHeader class="flex items-center justify-between">
      <CardTitle>
        <TitleStatus
          :title="props.domain.name"
          :loading="!props.application.domains_synced"
          :icon="props.application.domains_synced ? CircleCheck : undefined"
        />
      </CardTitle>
      <CardAction>
        <ButtonGroup class="space-x-1">
          <Button size="sm" variant="outline" @click="openEditDomainDialog" class="gap-2">
            <Edit />
            Edit
          </Button>
          <ConfirmationButton
            title="Domain"
            mode="delete"
            :description="props.domain.name"
            :onConfirm="onClickConfirmDelete"
          />
        </ButtonGroup>
      </CardAction>
    </CardHeader>
    <CardFooter class="border-t">
      <Button size="sm" variant="outline" class="w-full" as-child>
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
            <FieldLabel for="edit-domain-name"> Name </FieldLabel>
            <InputGroup>
              <InputGroupAddon>
                <InputGroupText>https://</InputGroupText>
              </InputGroupAddon>
              <InputGroupInput
                id="edit-domain-name"
                v-model="domain.name"
                placeholder="subdomain"
                class="!pl-0.5"
              />
              <InputGroupAddon align="inline-end">
                <InputGroupText
                  >.{{ domain.type == 'internal' ? 'int.' : ''
                  }}{{ appStore.info!.domain }}</InputGroupText
                >
              </InputGroupAddon>
            </InputGroup>
          </Field>
          <Field>
            <FieldLabel for="edit-domain-type"> Type </FieldLabel>
            <Select v-model="domain.type">
              <SelectTrigger id="edit-domain-type">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="internal"> Internal </SelectItem>
                <SelectItem value="public"> Public </SelectItem>
              </SelectContent>
            </Select>
          </Field>
          <Field>
            <FieldLabel for="edit-domain-port"> Port </FieldLabel>
            <Input id="edit-domain-port" v-model="domain.port" type="number" placeholder="80" />
          </Field>
          <Field>
            <FieldError v-if="editDomainErrorMessage">{{ editDomainErrorMessage }}</FieldError>
          </Field>
        </FieldGroup>
      </FieldSet>
      <DialogFooter>
        <Button
          size="sm"
          type="button"
          variant="outline"
          @click="isEditDomainDialogOpen = false"
          :disabled="isClickedEditDomainConfirm"
        >
          Cancel
        </Button>
        <Button
          size="sm"
          @click="onClickEditDomainConfirm"
          :disabled="isClickedEditDomainConfirm || !domain.name.trim()"
        >
          <Spinner class="animate-spin" v-if="isClickedEditDomainConfirm" />
          Save
        </Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>
