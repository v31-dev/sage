<script setup lang="ts">
import { ref } from 'vue';
import { toast } from 'vue-sonner';
import { Plus } from 'lucide-vue-next';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from '@/components/ui/dialog';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Field, FieldGroup, FieldLabel, FieldSet, FieldError } from '@/components/ui/field';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { Spinner } from '@/components/ui/spinner';

import { type Application, type Domain, getDomainAPI } from '@/services/api';

interface Props {
  application: Application;
  domainAPI: ReturnType<typeof getDomainAPI>;
  loadApplication: () => Promise<void>;
}

const props = withDefaults(defineProps<Props>(), {});

const isAddDomainDialogOpen = ref(false);
const domain = ref({
  name: '',
  type: 'internal' as 'internal' | 'public',
  port: 80,
});
const addDomainErrorMessage = ref('');
const isClickedAddDomainConfirm = ref(false);

function openAddDomainDialog() {
  addDomainErrorMessage.value = '';
  domain.value = {
    name: '',
    type: 'internal',
    port: 80,
  };
  isAddDomainDialogOpen.value = true;
}

async function onClickAddDomainConfirm() {
  addDomainErrorMessage.value = '';

  if (!domain.value.name.trim()) {
    addDomainErrorMessage.value = 'Please enter a domain name';
    return;
  }

  try {
    isClickedAddDomainConfirm.value = true;
    (await props.domainAPI.create(domain.value)) as Domain;
    isAddDomainDialogOpen.value = false;
    await props.loadApplication();
    toast.success('Domain added successfully');
  } catch (err) {
    addDomainErrorMessage.value = err instanceof Error ? err.message : 'Failed to add domain';
  } finally {
    isClickedAddDomainConfirm.value = false;
  }
}
</script>

<template>
  <Dialog v-model:open="isAddDomainDialogOpen">
    <DialogTrigger asChild>
      <Button size="sm" @click="openAddDomainDialog" class="gap-2">
        <Plus />
        Add Domain
      </Button>
    </DialogTrigger>
    <DialogContent class="sm:max-w-[400px]">
      <DialogHeader>
        <DialogTitle>Add Domain</DialogTitle>
        <DialogDescription>
          {{ props.application.name }}
        </DialogDescription>
      </DialogHeader>
      <FieldSet>
        <FieldGroup>
          <Field>
            <FieldLabel for="domain-name"> Name </FieldLabel>
            <Input id="domain-name" v-model="domain.name" placeholder="subdomain" />
          </Field>
          <Field>
            <FieldLabel for="domain-type"> Type </FieldLabel>
            <Select v-model="domain.type">
              <SelectTrigger id="domain-type">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="internal"> Internal </SelectItem>
                <SelectItem value="public"> Public </SelectItem>
              </SelectContent>
            </Select>
          </Field>
          <Field>
            <FieldLabel for="domain-port"> Port </FieldLabel>
            <Input id="domain-port" v-model="domain.port" type="number" />
          </Field>
          <Field>
            <FieldError v-if="addDomainErrorMessage">{{ addDomainErrorMessage }}</FieldError>
          </Field>
        </FieldGroup>
      </FieldSet>
      <DialogFooter>
        <Button
          size="sm"
          type="button"
          variant="outline"
          @click="isAddDomainDialogOpen = false"
          :disabled="isClickedAddDomainConfirm"
        >
          Cancel
        </Button>
        <Button
          size="sm"
          @click="onClickAddDomainConfirm"
          :disabled="isClickedAddDomainConfirm || !domain.name.trim()"
        >
          <Spinner class="animate-spin" v-if="isClickedAddDomainConfirm" />
          Add
        </Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>
