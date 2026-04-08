<script setup lang="ts">
import { ref, onMounted } from 'vue';
import { useRouter } from 'vue-router';
import { Activity, ChevronRight, MoreVertical } from 'lucide-vue-next';
import { Card, CardContent } from '@/components/ui/card';
import { Spinner } from '@/components/ui/spinner';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';

import { workersAPI, fetchWorkerMetrics, type Worker } from '@/services/api';

const router = useRouter();
const workers = ref<Worker[]>([]);
const isLoading = ref(true);
const workerMetrics = ref<Record<string, any>>({});

onMounted(async () => {
  try {
    workers.value = (await workersAPI.fetchAll()) as Worker[];

    // Fetch latest metrics for each worker
    for (const worker of workers.value) {
      try {
        const metrics = await fetchWorkerMetrics(worker.hostname, '1m');
        if (metrics.host.length > 0) {
          const latest = metrics.host[metrics.host.length - 1];
          if (latest) {
            workerMetrics.value[worker.hostname] = {
              cpu: latest.cpu_pct,
              mem: latest.mem_used_mb,
              memTotal: metrics.meta.mem_total_mb,
              disk: latest.disk_used_gb,
              diskTotal: metrics.meta.disk_total_gb,
              netRx: latest.net_rx_kbps ? Math.round((latest.net_rx_kbps / 1000) * 10) / 10 : 0,
              netTx: latest.net_tx_kbps ? Math.round((latest.net_tx_kbps / 1000) * 10) / 10 : 0,
              containers: Object.keys(metrics.containers ?? {}).length,
            };
          }
        }
      } catch (err) {
        // Skip on error
      }
    }
    isLoading.value = false;
  } catch (err) {
    isLoading.value = false;
  }
});

function goToMetrics(hostname: string) {
  router.push(`/workers/${hostname}/metrics`);
}

function getStatusColor(percent: number) {
  if (percent < 50) return 'bg-green-500';
  if (percent < 70) return 'bg-yellow-500';
  return 'bg-red-500';
}

function getStatusTextColor(percent: number) {
  if (percent < 50) return 'text-green-600';
  if (percent < 70) return 'text-yellow-600';
  return 'text-red-600';
}
</script>

<template>
  <main class="flex-1 px-0 sm:px-4 py-4 sm:py-4 relative">
    <div class="max-w-7xl mx-auto">
      <!-- Loading State -->
      <div v-if="isLoading" class="absolute inset-0 z-50 flex items-center justify-center">
        <div class="flex flex-col items-center gap-4">
          <Spinner />
          <p class="text-sm text-muted-foreground">Loading workers...</p>
        </div>
      </div>

      <!-- Content -->
      <div v-else class="space-y-6">
        <!-- Empty State -->
        <Card v-if="workers.length === 0">
          <CardContent class="flex flex-col items-center justify-center py-12">
            <p class="text-muted-foreground text-lg">No workers found</p>
          </CardContent>
        </Card>

        <!-- Workers Table -->
        <Card v-else class="py-0">
          <Table>
            <TableHeader class="bg-muted/50">
              <TableRow class="hover:bg-muted/50">
                <TableHead class="w-8 pl-4 py-2"></TableHead>
                <TableHead class="w-24 sm:w-32 py-2">Hostname</TableHead>
                <TableHead class="hidden sm:table-cell w-24 py-2">IP</TableHead>
                <TableHead class="w-20 sm:w-24 py-2">CPU</TableHead>
                <TableHead class="w-24 sm:w-28 py-2">Memory</TableHead>
                <TableHead class="hidden sm:table-cell w-24 py-2">Disk</TableHead>
                <TableHead class="hidden md:table-cell w-32 py-2">Network</TableHead>
                <TableHead class="hidden md:table-cell text-center w-20 py-2">Containers</TableHead>
                <TableHead class="w-10 py-2"></TableHead>
                <TableHead class="w-10 pr-4 py-2"></TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              <TableRow v-for="worker in workers" :key="worker.hostname" class="hover:bg-muted/50">
                <!-- Online Status -->
                <TableCell class="pl-4">
                  <div
                    :class="['w-2 h-2 rounded-full', worker.online ? 'bg-green-500' : 'bg-red-500']"
                  ></div>
                </TableCell>

                <!-- Hostname -->
                <TableCell class="font-medium text-sm truncate">{{ worker.hostname }}</TableCell>

                <!-- IP -->
                <TableCell
                  class="hidden sm:table-cell font-mono text-xs text-muted-foreground truncate"
                  >{{ worker.ip }}</TableCell
                >

                <!-- CPU Bar -->
                <TableCell>
                  <div v-if="workerMetrics[worker.hostname]" class="space-y-1">
                    <div class="flex justify-between items-center mb-1">
                      <span
                        class="text-xs font-semibold"
                        :class="getStatusTextColor(workerMetrics[worker.hostname].cpu)"
                      >
                        {{ Math.round(workerMetrics[worker.hostname].cpu * 10) / 10 }}%
                      </span>
                    </div>
                    <div class="w-full bg-muted rounded-full h-1.5">
                      <div
                        :class="[
                          'h-1.5 rounded-full transition-all duration-300',
                          getStatusColor(workerMetrics[worker.hostname].cpu),
                        ]"
                        :style="{ width: Math.min(workerMetrics[worker.hostname].cpu, 100) + '%' }"
                      ></div>
                    </div>
                  </div>
                  <div v-else class="text-xs text-muted-foreground">-</div>
                </TableCell>

                <!-- Memory Bar -->
                <TableCell>
                  <div v-if="workerMetrics[worker.hostname]" class="space-y-1">
                    <div class="flex justify-between items-center mb-1">
                      <span
                        class="text-xs font-semibold"
                        :class="
                          getStatusTextColor(
                            (workerMetrics[worker.hostname].mem /
                              workerMetrics[worker.hostname].memTotal) *
                              100
                          )
                        "
                      >
                        {{
                          Math.round(
                            (workerMetrics[worker.hostname].mem /
                              workerMetrics[worker.hostname].memTotal) *
                              10
                          ) / 10
                        }}%
                      </span>
                    </div>
                    <div class="w-full bg-muted rounded-full h-1.5">
                      <div
                        :class="[
                          'h-1.5 rounded-full transition-all duration-300',
                          getStatusColor(
                            (workerMetrics[worker.hostname].mem /
                              workerMetrics[worker.hostname].memTotal) *
                              100
                          ),
                        ]"
                        :style="{
                          width:
                            Math.min(
                              (workerMetrics[worker.hostname].mem /
                                workerMetrics[worker.hostname].memTotal) *
                                100,
                              100
                            ) + '%',
                        }"
                      ></div>
                    </div>
                  </div>
                  <div v-else class="text-xs text-muted-foreground">-</div>
                </TableCell>

                <!-- Disk Bar -->
                <TableCell class="hidden sm:table-cell">
                  <div v-if="workerMetrics[worker.hostname]" class="space-y-1">
                    <div class="flex justify-between items-center mb-1">
                      <span
                        class="text-xs font-semibold"
                        :class="
                          getStatusTextColor(
                            (workerMetrics[worker.hostname].disk /
                              workerMetrics[worker.hostname].diskTotal) *
                              100
                          )
                        "
                      >
                        {{
                          Math.round(
                            (workerMetrics[worker.hostname].disk /
                              workerMetrics[worker.hostname].diskTotal) *
                              10
                          ) / 10
                        }}%
                      </span>
                    </div>
                    <div class="w-full bg-muted rounded-full h-1.5">
                      <div
                        :class="[
                          'h-1.5 rounded-full transition-all duration-300',
                          getStatusColor(
                            (workerMetrics[worker.hostname].disk /
                              workerMetrics[worker.hostname].diskTotal) *
                              100
                          ),
                        ]"
                        :style="{
                          width:
                            Math.min(
                              (workerMetrics[worker.hostname].disk /
                                workerMetrics[worker.hostname].diskTotal) *
                                100,
                              100
                            ) + '%',
                        }"
                      ></div>
                    </div>
                  </div>
                  <div v-else class="text-xs text-muted-foreground">-</div>
                </TableCell>

                <!-- Network -->
                <TableCell class="hidden md:table-cell">
                  <div v-if="workerMetrics[worker.hostname]" class="text-xs space-y-0.5">
                    <p class="font-mono">
                      <span class="text-blue-600 dark:text-blue-400">↓</span>
                      {{ workerMetrics[worker.hostname].netRx }} Mbps
                    </p>
                    <p class="font-mono">
                      <span class="text-orange-600 dark:text-orange-400">↑</span>
                      {{ workerMetrics[worker.hostname].netTx }} Mbps
                    </p>
                  </div>
                  <div v-else class="text-xs text-muted-foreground">-</div>
                </TableCell>

                <!-- Containers -->
                <TableCell class="hidden md:table-cell text-center">
                  <div v-if="workerMetrics[worker.hostname]" class="font-semibold">
                    {{ workerMetrics[worker.hostname].containers }}
                  </div>
                  <div v-else class="text-xs text-muted-foreground">-</div>
                </TableCell>

                <!-- Navigate Button -->
                <TableCell class="text-center">
                  <button
                    @click="router.push(`/workers/${worker.hostname}`)"
                    class="p-1.5 text-muted-foreground hover:text-primary hover:bg-muted transition-colors rounded-md"
                    title="View Worker Details"
                  >
                    <ChevronRight class="w-4 h-4" />
                  </button>
                </TableCell>

                <!-- Actions Dropdown Menu -->
                <TableCell class="text-right pr-4">
                  <DropdownMenu>
                    <DropdownMenuTrigger as-child>
                      <button
                        class="p-1.5 text-muted-foreground hover:text-primary hover:bg-muted transition-colors rounded-md"
                        title="Actions"
                      >
                        <MoreVertical class="w-4 h-4" />
                      </button>
                    </DropdownMenuTrigger>
                    <DropdownMenuContent align="end" class="w-48">
                      <DropdownMenuItem @click="goToMetrics(worker.hostname)">
                        <Activity class="w-4 h-4 mr-2" />
                        View Metrics
                      </DropdownMenuItem>
                    </DropdownMenuContent>
                  </DropdownMenu>
                </TableCell>
              </TableRow>
            </TableBody>
          </Table>
        </Card>
      </div>
    </div>
  </main>
</template>
