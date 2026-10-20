// What a state looks like, by meaning. Colours come from the theme tokens in style.css
// (--success, --warning, --info, --destructive), so light and dark follow the theme. Every
// place that shows a status picks one of these meanings and uses its classes.
export type Tone = 'success' | 'warning' | 'error' | 'info' | 'neutral'

interface ToneClasses {
  // Coloured text or icon
  text: string
  // A bordered, tinted chip
  chip: string
  // A tinted background with matching text, for an icon in a circle or a row
  soft: string
  // A solid dot
  dot: string
  // The fill of a Progress bar
  bar: string
}

export const TONES: Record<Tone, ToneClasses> = {
  success: {
    text: 'text-success',
    chip: 'border-success/30 bg-success/10 text-success',
    soft: 'bg-success/10 text-success',
    dot: 'bg-success',
    bar: '[&>div]:bg-success',
  },
  warning: {
    text: 'text-warning',
    chip: 'border-warning/30 bg-warning/10 text-warning',
    soft: 'bg-warning/10 text-warning',
    dot: 'bg-warning',
    bar: '[&>div]:bg-warning',
  },
  error: {
    text: 'text-destructive',
    chip: 'border-destructive/30 bg-destructive/10 text-destructive',
    soft: 'bg-destructive/10 text-destructive',
    dot: 'bg-destructive',
    bar: '[&>div]:bg-destructive',
  },
  info: {
    text: 'text-info',
    chip: 'border-info/30 bg-info/10 text-info',
    soft: 'bg-info/10 text-info',
    dot: 'bg-info',
    bar: '[&>div]:bg-info',
  },
  neutral: {
    text: 'text-muted-foreground',
    chip: 'border-border bg-muted text-muted-foreground',
    soft: 'bg-muted text-muted-foreground',
    dot: 'bg-muted-foreground',
    bar: '[&>div]:bg-muted-foreground',
  },
}
