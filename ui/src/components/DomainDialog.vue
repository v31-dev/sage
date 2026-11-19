<script setup lang="ts">
import { computed, ref } from 'vue'
import { toast } from 'vue-sonner'
import { Edit, Plus } from 'lucide-vue-next'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from '@/components/ui/dialog'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import {
  InputGroup,
  InputGroupAddon,
  InputGroupInput,
  InputGroupText,
} from '@/components/ui/input-group'
import {
  Field,
  FieldDescription,
  FieldError,
  FieldGroup,
  FieldLabel,
  FieldSet,
} from '@/components/ui/field'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { Spinner } from '@/components/ui/spinner'
import IconButton from '@/components/IconButton.vue'
import { useAppStore } from '@/stores/app'
import { type Domain, createDomain, updateDomain } from '@/services/api'

// Adds a domain to `container`, or edits `domain`, which stays on its container. The container's worker gives an
// internal or TCP name its subdomain.
interface Props {
  container: { id: string; worker: string }
  domain?: Domain
  reload: () => Promise<void>
}

const props = defineProps<Props>()
const appStore = useAppStore()

const GUIDANCE: Record<Domain['type'], string> = {
  public: "Reached from the internet at its own name, through its worker's tunnel.",
  internal: "Reached on the tailnet only, under its worker's subdomain.",
  tcp: "Reached on the tailnet only, on port 8443, under its worker's subdomain. Clients connect with TLS.",
}

const open = ref(false)
const type = ref<Domain['type'] | ''>('')
const name = ref('')
const port = ref(80)
const service = ref('')
const pool = ref('')
const corsOrigins = ref('')
const errorMessage = ref('')
const submitting = ref(false)

const subdomain = computed(
  () =>
    appStore.workers?.find(worker => worker.hostname === props.container.worker)?.subdomain ??
    '<subdomain>'
)
const prefix = computed(() => (type.value === 'tcp' ? '' : 'https://'))
const suffix = computed(() => {
  const base = appStore.info?.domain ?? ''
  if (type.value === 'public') return `.${base}`
  return `.${subdomain.value}.int.${base}${type.value === 'tcp' ? ':8443' : ''}`
})
const ready = computed(() => !!type.value && !!name.value.trim())

function openDialog() {
  errorMessage.value = ''
  type.value = props.domain?.type ?? ''
  name.value = props.domain?.name ?? ''
  port.value = props.domain?.port ?? 80
  service.value = props.domain?.service ?? ''
  pool.value = props.domain?.pool ?? ''
  corsOrigins.value = props.domain?.cors_origins?.join('\n') ?? ''
  open.value = true
}

async function onConfirm() {
  errorMessage.value = ''
  submitting.value = true
  try {
    const data = {
      name: name.value.trim(),
      type: type.value,
      port: Number(port.value),
      service: service.value.trim(),
      pool: pool.value.trim(),
      cors_origins:
        type.value === 'tcp' || !corsOrigins.value.trim()
          ? null
          : corsOrigins.value
              .split('\n')
              .map(origin => origin.trim())
              .filter(Boolean),
    }
    if (props.domain) await updateDomain(props.domain.id, data)
    else await createDomain({ ...data, container: props.container.id })
    open.value = false
    await props.reload()
    toast.success(props.domain ? 'Domain updated' : 'Domain added')
  } catch (err) {
    errorMessage.value = err instanceof Error ? err.message : 'Failed to save the domain'
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <Dialog v-model:open="open">
    <DialogTrigger v-if="props.domain" as-child>
      <IconButton :icon="Edit" label="Edit" @click="openDialog" />
    </DialogTrigger>
    <DialogTrigger v-else as-child>
      <IconButton :icon="Plus" label="Add domain" @click="openDialog" />
    </DialogTrigger>
    <DialogContent class="sm:max-w-[480px]">
      <DialogHeader>
        <DialogTitle>{{ props.domain ? 'Edit Domain' : 'Add Domain' }}</DialogTitle>
        <DialogDescription>{{
          props.domain?.host ?? `A name for this container on ${props.container.worker}`
        }}</DialogDescription>
      </DialogHeader>
      <FieldSet>
        <FieldGroup>
          <Field>
            <FieldLabel for="domain-type"> Type </FieldLabel>
            <Select v-model="type">
              <SelectTrigger id="domain-type">
                <SelectValue placeholder="Choose a type" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="public"> Public </SelectItem>
                <SelectItem value="internal"> Internal </SelectItem>
                <SelectItem value="tcp"> TCP </SelectItem>
              </SelectContent>
            </Select>
            <FieldDescription v-if="type">{{ GUIDANCE[type] }}</FieldDescription>
          </Field>
          <Field>
            <FieldLabel for="domain-name"> Name </FieldLabel>
            <InputGroup>
              <InputGroupAddon v-if="prefix">
                <InputGroupText>{{ prefix }}</InputGroupText>
              </InputGroupAddon>
              <InputGroupInput
                id="domain-name"
                v-model="name"
                placeholder="e.g. shop"
                :disabled="!type"
                :class="prefix && '!pl-0.5'"
              />
              <InputGroupAddon v-if="type" align="inline-end">
                <InputGroupText>{{ suffix }}</InputGroupText>
              </InputGroupAddon>
            </InputGroup>
            <FieldDescription>Lowercase letters, digits and dashes.</FieldDescription>
          </Field>
          <Field>
            <FieldLabel for="domain-port"> Port </FieldLabel>
            <Input id="domain-port" v-model="port" type="number" />
            <FieldDescription>The container's port the name routes to.</FieldDescription>
          </Field>
          <Field v-if="type && type !== 'tcp'">
            <FieldLabel for="domain-cors"> Allowed origins (optional) </FieldLabel>
            <Textarea
              id="domain-cors"
              v-model="corsOrigins"
              placeholder="e.g. https://app.example.com"
              :rows="2"
            />
            <FieldDescription>
              One origin per line, or * for any origin. Leave blank to allow only
              {{ name.trim() ? `${prefix}${name.trim()}${suffix}` : "this domain's own origin" }}.
              References to other domains must be updated manually when their names change.
            </FieldDescription>
          </Field>
          <Field>
            <FieldLabel for="domain-service"> Service (optional) </FieldLabel>
            <Input id="domain-service" v-model="service" placeholder="e.g. shop" />
            <FieldDescription>
              Names that share a service are listed together at discovery, apart for public and
              internal.
            </FieldDescription>
          </Field>
          <Field>
            <FieldLabel for="domain-pool"> Pool (optional) </FieldLabel>
            <Input id="domain-pool" v-model="pool" placeholder="e.g. eu" />
            <FieldDescription>A group within the service.</FieldDescription>
          </Field>
          <Field>
            <FieldError v-if="errorMessage">{{ errorMessage }}</FieldError>
          </Field>
        </FieldGroup>
      </FieldSet>
      <DialogFooter>
        <Button size="sm" variant="outline" :disabled="submitting" @click="open = false">
          Cancel
        </Button>
        <Button size="sm" :disabled="submitting || !ready" @click="onConfirm">
          <Spinner v-if="submitting" class="animate-spin" />
          {{ props.domain ? 'Save' : 'Add' }}
        </Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>
