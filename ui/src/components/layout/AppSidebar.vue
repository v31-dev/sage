<script setup lang="ts">
import type { SidebarProps } from '@/components/ui/sidebar'
import { useRoute } from 'vue-router'
import { onMounted, watch } from 'vue'
import {
  Activity,
  Bell,
  Briefcase,
  Building,
  CircleArrowUp,
  Cloud,
  ExternalLink,
  HardDrive,
  Home,
  ListTodo,
  Logs,
  Network,
  Server,
  CloudBackup,
} from 'lucide-vue-next'
import {
  Sidebar,
  SidebarContent,
  SidebarHeader,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarGroup,
  SidebarGroupLabel,
  useSidebar,
} from '@/components/ui/sidebar'
import SettingStateIndicator from '@/components/SettingStateIndicator.vue'
import { SETTING_STATES } from '@/lib/settingState'
import type { SettingSection } from '@/services/api'
import { useAppStore } from '@/stores/app'
import { useSettingsStatusStore } from '@/stores/settingsStatus'

const route = useRoute()
const appStore = useAppStore()
const settingsStatus = useSettingsStatusStore()
const { isMobile, setOpenMobile, state: sidebarState } = useSidebar()

const props = withDefaults(defineProps<SidebarProps>(), {
  collapsible: 'icon',
})

interface MenuInterface {
  name: string
  path?: string
  url?: string
  icon?: any
  section?: SettingSection
  children?: MenuInterface[]
}

const menu: MenuInterface[] = [
  {
    name: 'Home',
    path: '/',
    icon: Home,
  },
  {
    name: 'Application',
    children: [
      {
        name: 'Projects',
        path: '/projects',
        icon: Briefcase,
      },
      {
        name: 'Requests',
        path: '/requests',
        icon: Activity,
      },
    ],
  },
  {
    name: 'Platform',
    children: [
      {
        name: 'Workers',
        path: '/workers',
        icon: Server,
      },
      {
        name: 'System',
        path: '/system',
        icon: Activity,
      },
      {
        name: 'Tasks',
        path: '/tasks',
        icon: ListTodo,
      },
      {
        name: 'Logs',
        path: '/logs',
        icon: Logs,
      },
      {
        name: 'Backups',
        path: '/backups',
        icon: CloudBackup,
      },
    ],
  },
  {
    name: 'Settings',
    children: [
      { name: 'Tailscale', path: '/tailscale', icon: Network, section: 'tailscale' },
      { name: 'Cloudflare', path: '/cloudflare', icon: Cloud, section: 'cloudflare' },
      { name: 'S3', path: '/s3', icon: HardDrive, section: 's3' },
      {
        name: 'Notifications',
        path: '/notifications',
        icon: Bell,
        section: 'notifications',
      },
      { name: 'Upgrade', path: '/upgrade', icon: CircleArrowUp },
    ],
  },
]

function sectionState(item: MenuInterface) {
  return item.section ? settingsStatus.status?.[item.section]?.state : undefined
}

function tooltip(item: MenuInterface) {
  const state = sectionState(item)
  return state ? `${item.name}: ${SETTING_STATES[state].label}` : item.name
}

onMounted(() => settingsStatus.start())

// Close sidebar on mobile when route changes
watch(
  () => route.path,
  () => {
    if (isMobile.value) {
      setOpenMobile(false)
    }
  }
)
</script>

<template>
  <Sidebar v-bind="props">
    <SidebarHeader>
      <div class="flex items-center gap-2 px-2 py-1.5">
        <Building class="size-5 shrink-0" />
        <div class="grid flex-1 text-left text-sm leading-tight">
          <span class="truncate font-semibold">
            {{ appStore.info?.name }}
          </span>
        </div>
      </div>
    </SidebarHeader>
    <SidebarContent>
      <SidebarGroup v-for="item in menu" :key="item.name">
        <SidebarGroupLabel v-if="Array.isArray(item.children)">{{ item.name }}</SidebarGroupLabel>
        <SidebarMenu>
          <SidebarMenuItem
            v-for="child in Array.isArray(item.children) ? item.children : [item]"
            :key="child.name || item.name"
          >
            <SidebarMenuButton
              v-if="'path' in child"
              as-child
              :tooltip="tooltip(child)"
              :isActive="
                route.path === child.path ||
                (child.path !== '/' && route.path.startsWith(child.path + '/'))
              "
            >
              <RouterLink :to="child.path!">
                <span class="relative flex">
                  <component :is="child.icon" class="size-4" />
                  <span
                    v-if="sidebarState === 'collapsed' && sectionState(child)"
                    :class="[
                      'absolute -top-0.5 -right-0.5 size-1.5 rounded-full',
                      SETTING_STATES[sectionState(child)!].dot,
                    ]"
                  />
                </span>
                <span>{{ child.name }}</span>
                <SettingStateIndicator
                  v-if="sectionState(child)"
                  :state="sectionState(child)!"
                  class="ml-auto"
                />
              </RouterLink>
            </SidebarMenuButton>
            <SidebarMenuButton v-else-if="'url' in child" as-child :tooltip="child.name">
              <a
                :href="child.url"
                :target="child.url?.startsWith('http') ? '_blank' : undefined"
                :rel="child.url?.startsWith('http') ? 'noopener noreferrer' : undefined"
              >
                <component :is="child.icon" />
                <span>{{ child.name }}</span>
                <ExternalLink v-if="child.url?.startsWith('http')" class="size-4 ml-auto" />
              </a>
            </SidebarMenuButton>
          </SidebarMenuItem>
        </SidebarMenu>
      </SidebarGroup>
    </SidebarContent>
  </Sidebar>
</template>
