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
import { useAppStore } from '@/stores/app'
import { type Domain, type LinkableContainer, createDomain, updateDomain } from '@/services/api'

// Creates a domain, or edits `domain`. The container comes first: it decides the worker, so the name's shape follows
// it, and choosing another one clears the type.
interface Props {
  containers: LinkableContainer[]
  domains: Domain[]
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
const containerId = ref('')
const type = ref<Domain['type'] | ''>('')
const name = ref('')
const port = ref(80)
const service = ref('')
const pool = ref('')
const corsOrigins = ref('')
const errorMessage = ref('')
const submitting = ref(false)

const container = computed(() =>
  props.containers.find(container => String(container.id) === containerId.value)
)
// The subdomain the name of an internal or TCP domain is under: its container's worker's, or for an unlinked one,
// the worker it kept.
const subdomain = computed(
  () =>
    container.value?.worker.subdomain ??
    appStore.workers?.find(worker => worker.hostname === props.domain?.worker)?.subdomain ??
    '<subdomain>'
)
const prefix = computed(() => (type.value === 'tcp' ? '' : 'https://'))
const suffix = computed(() => {
  const base = appStore.info?.domain ?? ''
  if (type.value === 'public') return `.${base}`
  return `.${subdomain.value}.int.${base}${type.value === 'tcp' ? ':8443' : ''}`
})
// The services already used in the namespace of the chosen type, to pick from.
const services = computed(() => {
  const isPublic = type.value === 'public'
  return [
    ...new Set(
      props.domains
        .filter(domain => (domain.type === 'public') === isPublic && domain.service)
        .map(domain => domain.service!)
    ),
  ].sort()
})
const ready = computed(
  () => !!type.value && !!name.value.trim() && (!!container.value || !!props.domain)
)

function openDialog() {
  errorMessage.value = ''
  containerId.value = props.domain?.container ? String(props.domain.container.id) : ''
  type.value = props.domain?.type ?? ''
  name.value = props.domain?.name ?? ''
  port.value = props.domain?.port ?? 80
  service.value = props.domain?.service ?? ''
  pool.value = props.domain?.pool ?? ''
  corsOrigins.value = props.domain?.cors_origins?.join('\n') ?? ''
  open.value = true
}

function chooseContainer(value: unknown) {
  containerId.value = String(value)
  type.value = ''
}

function containerLabel(container: LinkableContainer) {
  return `${container.application.label} (${container.application.project.label}) · ${container.worker.hostname}`
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
      ...(container.value ? { container: container.value.id } : {}),
    }
    if (props.domain) await updateDomain(props.domain.id, data)
    else await createDomain(data)
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
    <DialogTrigger as-child>
      <Button v-if="props.domain" size="sm" variant="outline" class="gap-2" @click="openDialog">
        <Edit />
        Edit
      </Button>
      <Button v-else size="sm" class="gap-2" @click="openDialog">
        <Plus />
        Add Domain
      </Button>
    </DialogTrigger>
    <DialogContent class="sm:max-w-[480px]">
      <DialogHeader>
        <DialogTitle>{{ props.domain ? 'Edit Domain' : 'Add Domain' }}</DialogTitle>
        <DialogDescription>{{
          props.domain?.host ?? 'A name for one container'
        }}</DialogDescription>
      </DialogHeader>
      <FieldSet>
        <FieldGroup>
          <Field>
            <FieldLabel for="domain-container"> Container </FieldLabel>
            <Select :model-value="containerId" @update:model-value="chooseContainer">
              <SelectTrigger id="domain-container">
                <SelectValue
                  :placeholder="
                    props.domain ? 'Not linked: choose a container' : 'Choose a container'
                  "
                />
              </SelectTrigger>
              <SelectContent>
                <SelectItem
                  v-for="item in props.containers"
                  :key="item.id"
                  :value="String(item.id)"
                >
                  {{ containerLabel(item) }}
                </SelectItem>
              </SelectContent>
            </Select>
          </Field>
          <Field>
            <FieldLabel for="domain-type"> Type </FieldLabel>
            <Select v-model="type" :disabled="!container && !props.domain">
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
            <Input
              id="domain-service"
              v-model="service"
              list="domain-services"
              placeholder="e.g. shop"
            />
            <datalist id="domain-services">
              <option v-for="item in services" :key="item" :value="item" />
            </datalist>
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
