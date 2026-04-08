<script setup lang="ts">
import type { ChartConfig } from '@/components/ui/chart';
import { VisArea, VisLine, VisAxis, VisXYContainer } from '@unovis/vue';
import { CurveType } from '@unovis/ts';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import {
  ChartContainer,
  ChartCrosshair,
  ChartTooltip,
  ChartTooltipContent,
  componentToString,
} from '@/components/ui/chart';
import { computed } from 'vue';

interface Series {
  key: string;
  name: string;
  tooltip_label: string;
  color: string;
  opacity?: number;
}

interface ChartDataPoint {
  date: Date;
  [key: string]: number | Date | string | null;
}

interface Props {
  title: string;
  data: ChartDataPoint[];
  series: Series[];
  unit: string;
  yMax?: number;
  height?: string;
  type: 'area' | 'line';
}

const props = withDefaults(defineProps<Props>(), {
  height: '300px',
  type: 'area',
});

const chartConfig = computed(() => {
  const config: ChartConfig = {};
  for (const s of props.series) {
    config[s.tooltip_label] = {
      label: s.name,
      color: s.color,
    };
  }
  return config;
});

const months = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

// Format date to DD MMM HH:mm AM?PM
function formatDateForTooltip(d: number | Date) {
  const date = d instanceof Date ? d : new Date(d);
  const hours = date.getHours();
  const minutes = date.getMinutes().toString().padStart(2, '0');
  const ampm = hours >= 12 ? 'PM' : 'AM';
  const hour12 = hours % 12 || 12;
  return `${date.getDate()} ${months[date.getMonth()]} ${hour12}:${minutes} ${ampm}`;
}

// Format date to HH:mm AM/PM
function formatDateForXAxis(d: number) {
  const date = new Date(d);
  const hours = date.getHours();
  const minutes = date.getMinutes().toString().padStart(2, '0');
  const ampm = hours >= 12 ? 'PM' : 'AM';
  const hour12 = hours % 12 || 12;
  return `${hour12}:${minutes} ${ampm}`;
}

function tooltipColor(_: any, i: number) {
  return props.series[i]?.color;
}
</script>

<template>
  <Card>
    <CardHeader class="pb-2">
      <CardTitle class="text-lg font-semibold">{{ title }}</CardTitle>
    </CardHeader>
    <CardContent :style="{ height }">
      <ChartContainer :config="chartConfig">
        <VisXYContainer :data="data" :margin="{ top: 10, bottom: 10 }" :yDomain="[0, props.yMax]">
          <template v-for="s in props.series" :key="s.key">
            <VisArea
              v-if="props.type == 'area'"
              :x="(d: ChartDataPoint) => d.date"
              :y="(d: ChartDataPoint) => d[s.key]"
              :color="s.color"
              :opacity="s.opacity ?? 0.4"
              :line="true"
              :lineColor="s.color"
              :lineWidth="1"
              :curveType="CurveType.StepAfter"
              :fallbackValue="undefined"
              :interpolateMissingData="false"
            />

            <VisLine
              v-if="props.type == 'line'"
              :x="(d: ChartDataPoint) => d.date"
              :y="(d: ChartDataPoint) => d[s.key] ?? undefined"
              :color="s.color"
              :fallbackValue="undefined"
              :interpolateMissingData="false"
            />
          </template>

          <VisAxis
            type="x"
            :x="(d: ChartDataPoint) => d.date"
            :num-ticks="5"
            :tick-format="formatDateForXAxis"
          />

          <VisAxis
            type="y"
            :num-ticks="3"
            :tick-format="(d: number) => `${d.toFixed(0)} ${props.unit}`"
          />

          <ChartTooltip />
          <ChartCrosshair
            :template="
              componentToString(chartConfig, ChartTooltipContent, {
                labelFormatter: formatDateForTooltip,
              })
            "
            :color="tooltipColor"
          />
        </VisXYContainer>
      </ChartContainer>
    </CardContent>
  </Card>
</template>
