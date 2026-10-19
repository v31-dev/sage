# Frontend

## Stack

- Vue 3
- TypeScript
- Vite
- Pinia
- Vue Router
- Tailwind CSS v4
- Reka UI
- Unovis

## App Shape

The frontend is a SPA under `ui/` that talks to the backend under `/api`.
In the production image, the backend serves the built SPA at `/` and keeps API traffic under `/api`.

The shell is assembled in `ui/src/App.vue`:

- sidebar layout
- header and footer
- loading overlay during boot
- reload dialog on initialization failure
- toast notifications

Initial app boot currently fetches platform info through the Pinia store in `ui/src/stores/app.ts`.

## Main Pages

Routes in `ui/src/router/index.ts` currently cover:

- `/`
- `/projects`
- `/projects/:projectId`
- `/projects/:projectId/:appId`
- `/projects/:projectId/:appId/logs`
- `/projects/:projectId/:appId/metrics`
- `/requests`
- `/system`
- `/logs`
- `/workers`
- `/workers/:hostname`
- `/workers/:hostname/metrics`
- `/backups`
- `/tailscale`, `/cloudflare`, `/s3`, `/notifications`, `/upgrade` (the settings pages, grouped under "Settings" in the sidebar)

## Main Feature Areas

- Home dashboard for platform posture, recent activity, and critical events
- Projects and applications
- Application containers, domains, and volumes
- Application logs and metrics
- Worker inventory and worker metrics
- Global logs view
- Backups and restore flows
- Settings: one page per section, listed under a "Settings" group in the sidebar

## Forms

- Placeholders are only examples, written "e.g. …". They never stand in for a label, a
  hint or a default (they vanish on typing and read as a filled-in value).
- A field with a default is prefilled with the actual value.
- Optional fields say "(optional)" in the label; required is the unmarked default.
- Search boxes and select prompts ("Search logs…", "Select backup") are prompts, not
  values, and are fine.

## Dates

Every date shown goes through `formatDate` or `formatDateStringAgo` (`lib/utils.ts`), never
raw. Both parse with `parseDate`: backend timestamps without a timezone are UTC; ones with
`Z` or an offset are read as given.

## Settings Pages

Tailscale and Cloudflare are env vars, so their pages use `SettingsSection` with `readonly` (status, values, resync actions, and a `links` slot for their dashboards: the manager's machine in the Tailscale console; the tunnel and DNS records in Cloudflare). S3 and notifications are editable: a page passes `SettingsSection.vue` its `field-keys`, and the section does the rest end to end (layout, status badge, load, Save / Test / Reset, inline error, unsaved-changes dialog and route-leave guard). The page's fields go in the default slot, which receives `fields` (the current values) and `updateField`. Errors show inline; successes as toasts. The sidebar
and the pages read one status store (`stores/settingsStatus.ts`) that polls
`GET /api/settings/status`: every 3 s while a section is applying, otherwise every 30 s
and on window focus. States and their icons are in `lib/settingState.ts`.

## API Layer

The frontend API types and CRUD helpers are defined mainly in:

- `ui/src/services/api.ts`
- `ui/src/lib/api.ts`
- `ui/src/lib/logs.ts`
- `ui/src/lib/metrics.ts`

The service typings mirror the backend models closely, including:

- projects
- applications
- containers
- domains
- volumes
- settings
- metrics payloads

## Notes

- The current frontend README is still the default Vite template and should be treated as stale.
- The real source of truth for frontend structure is the code under `ui/src/`.
