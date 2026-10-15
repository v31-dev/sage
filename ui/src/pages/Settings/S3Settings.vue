<script setup lang="ts">
import { computed } from 'vue'
import { ChevronDown } from 'lucide-vue-next'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible'
import { Input } from '@/components/ui/input'
import CustomInput from '@/components/CustomInput.vue'
import { useAppStore } from '@/stores/app'
import SettingField from './SettingField.vue'
import SettingsSection from './SettingsSection.vue'
import { useSettingForm } from './useSettingForm'

const appStore = useAppStore()
const form = useSettingForm('s3', ['access_key', 'secret_key', 'bucket', 'endpoint', 'path'], 'S3')
const { fields, isLoading, saving, testing, error, dirty, leaveOpen } = form

const fernetDecrypt =
  "import os,sys; from cryptography.fernet import Fernet; sys.stdout.buffer.write(Fernet(os.environ['ENCRYPTION_KEY']).decrypt(sys.stdin.buffer.read()))"

const volumeCommand = `head -c -32 <timestamp>.tar.gz.enc \\
  | openssl enc -d -aes-256-cbc -pbkdf2 -pass env:ENCRYPTION_KEY \\
  | tar -xz`
const platformPythonCommand = `python3 -c "${fernetDecrypt}" < <timestamp>.db.enc > data.db`
const platformDockerCommand = computed(
  () => `docker run --rm -i -e ENCRYPTION_KEY --entrypoint python \\
  ghcr.io/v31-dev/sage:${appStore.info?.version ?? '<version>'} \\
  -c "${fernetDecrypt}" < <timestamp>.db.enc > data.db`
)
</script>

<template>
  <SettingsSection
    title="S3"
    description="Storage for platform and volume backups, on any S3-compatible service. Optional: without it, backups are off. Testing writes and deletes a small marker object."
    section="s3"
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
    <SettingField id="s3-access-key" label="Access Key">
      <Input
        id="s3-access-key"
        :model-value="fields.access_key"
        @update:model-value="value => form.updateField('access_key', value)"
      />
    </SettingField>
    <SettingField id="s3-secret-key" label="Secret Key">
      <CustomInput
        id="s3-secret-key"
        secret
        :model-value="fields.secret_key"
        @update:model-value="value => form.updateField('secret_key', value)"
      />
    </SettingField>
    <SettingField id="s3-bucket" label="Bucket">
      <Input
        id="s3-bucket"
        :model-value="fields.bucket"
        @update:model-value="value => form.updateField('bucket', value)"
        placeholder="e.g. sage-backups"
      />
    </SettingField>
    <SettingField
      id="s3-endpoint"
      label="Endpoint"
      description="URL of the S3 or S3-compatible service."
    >
      <Input
        id="s3-endpoint"
        :model-value="fields.endpoint"
        @update:model-value="value => form.updateField('endpoint', value)"
        type="url"
        placeholder="e.g. https://s3.eu-central-1.amazonaws.com"
      />
    </SettingField>
    <SettingField
      id="s3-path"
      label="Base Path"
      description="Prefix inside the bucket for all of Sage's backups."
    >
      <Input
        id="s3-path"
        :model-value="fields.path"
        @update:model-value="value => form.updateField('path', value)"
        placeholder="e.g. sage"
      />
    </SettingField>

    <template #after>
      <Card>
        <Collapsible>
          <CardHeader>
            <CollapsibleTrigger as-child>
              <Button
                variant="ghost"
                class="h-auto w-full justify-between p-0 has-[>svg]:px-0 hover:bg-transparent"
              >
                <CardTitle>Decrypting a backup by hand</CardTitle>
                <ChevronDown class="size-4" />
              </Button>
            </CollapsibleTrigger>
            <CardDescription>
              Backups are encrypted with the <code>ENCRYPTION_KEY</code> Sage runs with. Export it
              in your shell first: <code>export ENCRYPTION_KEY=...</code>
            </CardDescription>
          </CardHeader>
          <CollapsibleContent>
            <CardContent class="space-y-4 pt-4 text-sm">
              <div class="space-y-2">
                <p class="font-medium">Volume backup (<code>&lt;timestamp&gt;.tar.gz.enc</code>)</p>
                <p class="text-muted-foreground">
                  Extracts the volume's files into the current directory. The last 32 bytes are an
                  integrity check (HMAC-SHA256) that this command skips; Sage's own restore verifies
                  it.
                </p>
                <pre
                  class="overflow-x-auto rounded-md bg-muted p-3 text-xs"
                ><code>{{ volumeCommand }}</code></pre>
              </div>
              <div class="space-y-2">
                <p class="font-medium">Platform backup (<code>&lt;timestamp&gt;.db.enc</code>)</p>
                <p class="text-muted-foreground">
                  Writes the SQLite database as <code>data.db</code>. With Python and the
                  <code>cryptography</code> package:
                </p>
                <pre
                  class="overflow-x-auto rounded-md bg-muted p-3 text-xs"
                ><code>{{ platformPythonCommand }}</code></pre>
                <p class="text-muted-foreground">Or with only Docker, inside the Sage image:</p>
                <pre
                  class="overflow-x-auto rounded-md bg-muted p-3 text-xs"
                ><code>{{ platformDockerCommand }}</code></pre>
              </div>
            </CardContent>
          </CollapsibleContent>
        </Collapsible>
      </Card>
    </template>
  </SettingsSection>
</template>
