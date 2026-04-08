<script setup lang="ts">
import { ref } from 'vue';
import { Separator } from '@/components/ui/separator';
import { SidebarTrigger } from '@/components/ui/sidebar';
import { Button } from '@/components/ui/button';
import { Icon } from '@iconify/vue';
import { useColorMode } from '@vueuse/core';
import { computed } from 'vue';
import { useRoute } from 'vue-router';
import {
  Breadcrumb,
  BreadcrumbItem,
  BreadcrumbLink,
  BreadcrumbList,
  BreadcrumbPage,
  BreadcrumbSeparator,
} from '@/components/ui/breadcrumb';

const route = useRoute();

const mode = useColorMode();
const notificationCount = ref(3);

const breadcrumbs = computed(() => {
  const segments = route.path.split('/').filter(Boolean);
  return segments.map((segment, index) => ({
    name: index === 0 ? segment.charAt(0).toUpperCase() + segment.slice(1) : segment,
    path: '/' + segments.slice(0, index + 1).join('/'),
    isLast: index === segments.length - 1,
  }));
});
</script>

<template>
  <header
    class="flex h-10 sticky top-0 z-40 shrink-0 items-center border-b border-border bg-background/95 px-4"
  >
    <div class="flex items-center gap-2 flex-1">
      <SidebarTrigger class="-ml-1" />
      <Separator orientation="vertical" class="mr-2 data-[orientation=vertical]:h-4" />
      <Breadcrumb>
        <BreadcrumbList>
          <template v-for="(crumb, index) in breadcrumbs" :key="crumb.path">
            <BreadcrumbSeparator v-if="index > 0" />
            <BreadcrumbItem>
              <BreadcrumbLink v-if="!crumb.isLast" as-child>
                <RouterLink :to="crumb.path">{{ crumb.name }}</RouterLink>
              </BreadcrumbLink>
              <BreadcrumbPage v-else>{{ crumb.name }}</BreadcrumbPage>
            </BreadcrumbItem>
          </template>
        </BreadcrumbList>
      </Breadcrumb>
    </div>
    <div class="ml-auto">
      <Button variant="ghost" size="icon" class="relative">
        <Icon icon="radix-icons:bell" class="h-[1rem] w-[1rem]" />
        <span
          v-if="notificationCount > 0"
          class="absolute top-0 right-0 w-4 h-4 bg-red-500 text-white text-xs rounded-full flex items-center justify-center font-bold"
        >
          {{ notificationCount > 9 ? '9+' : notificationCount }}
        </span>
      </Button>
    </div>
    <div class="ml-auto">
      <Button variant="ghost" size="icon" @click="mode = mode === 'light' ? 'dark' : 'light'">
        <Icon
          icon="radix-icons:moon"
          class="h-[1rem] w-[1rem] rotate-0 scale-100 transition-all dark:-rotate-90 dark:scale-0"
        />
        <Icon
          icon="radix-icons:sun"
          class="absolute h-[1rem] w-[1rem] rotate-90 scale-0 transition-all dark:rotate-0 dark:scale-100"
        />
      </Button>
    </div>
  </header>
</template>
