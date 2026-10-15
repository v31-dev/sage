import { CircleCheck, CircleDashed, LoaderCircle, OctagonX, TriangleAlert } from 'lucide-vue-next'
import type { SettingState } from '@/services/api'

// Each state has its own icon shape as well as a colour, so colour is never the only cue.
export const SETTING_STATES: Record<
  SettingState,
  { label: string; icon: typeof CircleCheck; text: string; dot: string }
> = {
  not_configured: {
    label: 'Not configured',
    icon: CircleDashed,
    text: 'text-muted-foreground',
    dot: 'bg-muted-foreground',
  },
  applying: { label: 'Applying', icon: LoaderCircle, text: 'text-blue-600', dot: 'bg-blue-600' },
  ok: { label: 'OK', icon: CircleCheck, text: 'text-green-600', dot: 'bg-green-600' },
  retrying: { label: 'Retrying', icon: TriangleAlert, text: 'text-amber-600', dot: 'bg-amber-500' },
  error: { label: 'Error', icon: OctagonX, text: 'text-destructive', dot: 'bg-destructive' },
}
