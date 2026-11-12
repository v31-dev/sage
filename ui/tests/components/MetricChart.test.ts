import { mount } from '@vue/test-utils'
import { expect, it, vi } from 'vitest'
import MetricChart from '@/components/MetricChart.vue'

const inline = { template: '<div><slot /></div>' }
vi.mock('@unovis/vue', () => ({
  VisXYContainer: { template: '<div><slot /></div>' },
  VisCrosshair: { name: 'CrosshairProbe', props: ['data'], template: '<div />' },
  VisTooltip: { template: '<div />' },
  VisStackedBar: { template: '<div />' },
  VisLine: { template: '<div />' },
  VisArea: { template: '<div />' },
  VisAxis: { template: '<div />' },
}))
const point = { date: new Date('2026-01-01T00:00:00Z'), errors: 2, warnings: null }

it.each(['bar', 'line', 'area'] as const)(
  'configures %s crosshair accessors with the series values',
  type => {
    const wrapper = mount(MetricChart, {
      props: {
        title: 'Counts',
        type,
        unit: '',
        data: [point],
        series: [
          { key: 'errors', name: 'Errors', color: 'red', tooltip_label: 'errors_label' },
          { key: 'warnings', name: 'Warnings', color: 'orange', tooltip_label: 'warnings_label' },
        ],
      },
      global: {
        stubs: {
          ChartContainer: inline,
          VisXYContainer: inline,
          ChartTooltip: true,
          VisStackedBar: true,
          VisLine: true,
          VisArea: true,
          VisAxis: true,
        },
      },
    })
    const props = wrapper.findComponent({ name: 'CrosshairProbe' }).vm.$attrs as {
      x: (datum: typeof point) => number
      y?: ((datum: typeof point) => number)[]
      yStacked?: ((datum: typeof point) => number)[]
    }
    expect(props.x(point)).toBe(point.date.getTime())
    const accessors = type === 'bar' ? props.yStacked : props.y
    expect(accessors!.map((read: (datum: typeof point) => number) => read(point))).toEqual([
      2,
      null,
    ])
    expect(type === 'bar' ? props.y : props.yStacked).toBeUndefined()
  }
)
