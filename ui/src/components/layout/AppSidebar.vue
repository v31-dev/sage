<script setup lang="ts">
import type { SidebarProps } from '@/components/ui/sidebar'
import { useRoute } from 'vue-router'
import { computed, watch } from 'vue'
import {
  Activity,
  Bell,
  Briefcase,
  Bug,
  Building,
  CircleArrowUp,
  Cloud,
  ExternalLink,
  HardDrive,
  Home,
  ListTodo,
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
import StateIndicator from '@/components/StateIndicator.vue'
import SocketStatus from '@/components/layout/SocketStatus.vue'
import type { Indicator } from '@/services/api'
import { useAppStore } from '@/stores/app'

const route = useRoute()
const appStore = useAppStore()
const { isMobile, setOpenMobile, state: sidebarState } = useSidebar()

const props = withDefaults(defineProps<SidebarProps>(), {
  collapsible: 'icon',
})

interface MenuInterface {
  name: string
  path?: string
  url?: string
  icon?: any
  indicator?: Indicator
  children?: MenuInterface[]
}

const menu = computed<MenuInterface[]>(() => [
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
        indicator: appStore.workersIndicator,
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
        name: 'Backups',
        path: '/backups',
        icon: CloudBackup,
      },
    ],
  },
  {
    name: 'Settings',
    children: [
      {
        name: 'Tailscale',
        path: '/tailscale',
        icon: Network,
        indicator: appStore.sectionIndicator('tailscale'),
      },
      {
        name: 'Cloudflare',
        path: '/cloudflare',
        icon: Cloud,
        indicator: appStore.sectionIndicator('cloudflare'),
      },
      { name: 'S3', path: '/s3', icon: HardDrive, indicator: appStore.sectionIndicator('s3') },
      {
        name: 'Notifications',
        path: '/notifications',
        icon: Bell,
        indicator: appStore.sectionIndicator('notifications'),
      },
      { name: 'Upgrade', path: '/upgrade', icon: CircleArrowUp },
      { name: 'Debug', path: '/debug', icon: Bug, indicator: appStore.debugIndicator },
    ],
  },
])

function tooltip(item: MenuInterface) {
  return item.indicator ? `${item.name}: ${item.indicator.label}` : item.name
}

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
        <span class="relative shrink-0">
          <Building class="size-5" />
          <span class="absolute -right-1 -top-1">
            <SocketStatus />
          </span>
        </span>
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
                  <StateIndicator
                    v-if="sidebarState === 'collapsed' && child.indicator"
                    v-bind="child.indicator"
                    dot
                    class="absolute -top-0.5 -right-0.5 size-1.5"
                  />
                </span>
                <span>{{ child.name }}</span>
                <StateIndicator v-if="child.indicator" v-bind="child.indicator" class="ml-auto" />
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
