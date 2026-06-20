<script setup lang="ts">
import type { SidebarProps } from '@/components/ui/sidebar'
import { useRoute } from 'vue-router'
import { watch } from 'vue'
import {
  Activity,
  Briefcase,
  Building,
  ExternalLink,
  Home,
  ListTodo,
  Logs,
  Server,
  Settings2,
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
import { useAppStore } from '@/stores/app'

const route = useRoute()
const appStore = useAppStore()
const { isMobile, setOpenMobile } = useSidebar()

const props = withDefaults(defineProps<SidebarProps>(), {
  collapsible: 'icon',
})

interface MenuInterface {
  name: string
  path?: string
  url?: string
  icon?: any
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
      {
        name: 'Settings',
        path: '/settings',
        icon: Settings2,
      },
    ],
  },
]

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
            {{ appStore.info?.domain }}
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
              :isActive="
                route.path === child.path ||
                (child.path !== '/' && route.path.startsWith(child.path + '/'))
              "
            >
              <RouterLink :to="child.path!">
                <component :is="child.icon" />
                <span>{{ child.name }}</span>
              </RouterLink>
            </SidebarMenuButton>
            <SidebarMenuButton v-else-if="'url' in child" as-child>
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
