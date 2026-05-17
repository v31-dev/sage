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
- `/settings`

## Main Feature Areas

- Projects and applications
- Application containers, domains, and volumes
- Application logs and metrics
- Worker inventory and worker metrics
- Global logs view
- Backups and restore flows
- Platform settings

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
