<script setup lang="ts">
import { Card, CardContent } from '@/components/ui/card'
import CodeBlock from '@/components/CodeBlock.vue'
import SettingsSection from './SettingsSection.vue'

const fields = [
  { key: 'access_key', label: 'Access Key' },
  { key: 'secret_key', label: 'Secret Key', secret: true },
  { key: 'bucket', label: 'Bucket', placeholder: 'e.g. sage-backups' },
  {
    key: 'endpoint',
    label: 'Endpoint',
    description: 'URL of the S3 or S3-compatible service.',
    type: 'url',
    placeholder: 'e.g. https://s3.eu-central-1.amazonaws.com',
  },
  {
    key: 'path',
    label: 'Base Path',
    description: "Prefix inside the bucket for all of Sage's backups.",
    placeholder: 'e.g. /sage',
  },
]

// Every backup has one format: AES-256 by openssl, then a 32-byte HMAC-SHA256 trailer.
// The key comes from the ENCRYPTION_KEY environment variable.
const exportCommand = `export ENCRYPTION_KEY='<key>'`
const volumeCommand = `mkdir -p restore
head -c -32 <timestamp>.tar.gz.enc \\
  | openssl enc -d -aes-256-cbc -pbkdf2 -pass env:ENCRYPTION_KEY \\
  | tar -xz -C restore`
const platformCommand = `head -c -32 <timestamp>.db.enc \\
  | openssl enc -d -aes-256-cbc -pbkdf2 -pass env:ENCRYPTION_KEY \\
  > data.db`
</script>

<template>
  <SettingsSection
    title="S3"
    description="Storage for platform and volume backups, on any S3-compatible service. Optional: without it, backups are off. Testing writes and deletes a small marker object."
    section="s3"
    :fields="fields"
  >
    <template #after>
      <Card>
        <CardContent class="space-y-4 pt-4 text-sm">
          <div class="space-y-2">
            <p class="text-muted-foreground">
              Backups are encrypted with the <code>ENCRYPTION_KEY</code> env, in one format: AES-256
              with <code>openssl</code>, then a 32-byte integrity check (HMAC-SHA256) at the end.
              Export the key in your shell before running these commands (a plain
              <code>ENCRYPTION_KEY=...</code> isn't visible to <code>openssl</code>):
            </p>
            <CodeBlock :code="exportCommand" />
          </div>
          <div class="space-y-2">
            <p class="font-medium">Volume backup (<code>&lt;timestamp&gt;.tar.gz.enc</code>)</p>
            <p class="text-muted-foreground">
              Extracts the volume's files into a <code>restore</code> folder, in a folder named
              after the volume. <code>head</code> drops the integrity check.
            </p>
            <CodeBlock :code="volumeCommand" />
          </div>
          <div class="space-y-2">
            <p class="font-medium">Platform backup (<code>&lt;timestamp&gt;.db.enc</code>)</p>
            <p class="text-muted-foreground">Writes the SQLite database as <code>data.db</code>:</p>
            <CodeBlock :code="platformCommand" />
          </div>
        </CardContent>
      </Card>
    </template>
  </SettingsSection>
</template>
