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

Initial app boot fetches `GET /api` through the Pinia store in `ui/src/stores/app.ts`. That store owns the manager's status and follows its `info` topic: the version, the queue state (the pause banner and the restore overlay) and the settings status (the sidebar indicators and the settings pages). It also follows the `workers` topic (`workers`, null until loaded, and `workersError` if the first load fails) without holding the app back: the sidebar count (`workersIndicator`), the Workers page and the worker dot of an application's container cards (`workerStatus(hostname)`) all read that one list. The manager pushes each change (see Pushed Updates below). After an action the page updates the store at once (`setState` for pause and resume, `setSettingApplying` after a save) and the next push confirms it, as with `applicationDeployStatus`, which the deploy, stop, backup and restore buttons set together with the statuses of the containers they touch (`containerStatus` for a card), and which the application header shows; deleting a container sets only that container (`updateContainerStatus`); `refresh()` fetches right away. A part the manager couldn't build comes as `null` and the store keeps the last value. The metrics views poll while they are open.

## Pushed Updates

`ui/src/lib/updates.ts` holds the one Socket.IO connection (`/api/socket.io`) and the topics followed over it. A page or store follows a state topic with `watchState(topic, load, onState)` or a feed with `watchFeed(topic, load, onRows)`, where `load` is the page's own REST call, and calls the returned `stop()` when it goes away; `first` settles with the first load, or rejects if it fails, so a page has its data without the socket. The manager sends nothing when a topic is followed, only each change after that, so the library loads again every time the socket connects (the first time and after every reconnect), once the manager has acknowledged the subscription: what changed before that is in the load. A state loaded before a newer one was pushed is dropped; a feed's rows are not, since they are merged. Reconnecting, and the fall back from WebSocket to long-polling, are Socket.IO's, and the UI has no polling of its own. The connection opens with the first watcher and closes when the last one stops. Topics so far, with what follows them:
- `info`: the app store (loading `GET /api`).
- `workers`: the app store (loading `GET /workers`): for each worker its status, ip, path and container count. The Workers page, the sidebar and the container cards read the store; the metrics on the page's cards are asked for when a worker is added or removed and every 60 s.
- `worker:<hostname>`: the Worker page (loading `GET /workers/{hostname}`): the worker with its containers, its stack and its status. The page has no timer; an action on a container reloads it at once through `loadWorkerStatus`.
- `application:<project>/<name>`: the Application page (loading the application route). The first state is the page's; each push after it updates the status, the containers, the counts, the dirty flag and the domain sync, and leaves the volumes, domains and variables to the reload after an action (`loadApplication`), so a form being edited is not replaced.
- `tasks`: the Tasks page, the Upgrade page (which tasks it waits for before a stop) and the restore overlay, which follows it only while restoring (loading `GET /tasks`).
- `task-history`, a feed: the Tasks page's completed list (loading `GET /tasks/history`). It merges rows by `task_id`, newest first by `created_at`, and keeps the newest 100.
- `backup-status`: the Create Backup and Discover Backups buttons, each mirroring its own flag and loading the backups again when its work ends (loading `POST /backups/platform_backup_status`).
- `release`: the Upgrade page (loading `GET /release`). A check for updates is over when a release with a new `fetched_at` is pushed, or after 60 s with an error.
- `notifications`, a feed: the header's list, whose `load` asks for the newest 20 while it has none and for the rows after its newest `created_at` otherwise, so it also recovers what it missed; it merges rows by id, newest first, so a row it has is not shown twice and one that arrives out of order lands in place. The bell's marker is on while the newest id is above the last one seen, which is kept in `localStorage` when the bell is opened.

The indicator at the top left of the sidebar (`components/layout/SocketStatus.vue`, on the icon of the manager's name) shows the connection from the reactive `connection` object of `lib/updates.ts`: connecting, connected over a WebSocket, connected over the slower fallback, or not connected. It pulses each time the manager's heartbeat (its ping, every 10 s) arrives, and its tooltip says when the last one was.

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
- Help for a field is an info icon beside its label (`FieldLabelHelp`), never inside the
  input. Inside an input, a leading icon marks a state (a lock on a read-only field) and
  trailing icons are actions on the value: at most two (copy, reveal, clear), reveal
  outermost.

## Status Colours

A state's look is chosen by its meaning, never by a palette colour. The theme tokens
`--success`, `--warning`, `--info` and `--destructive` are in `src/style.css` (light and dark,
next to the shadcn tokens, and available as `text-success`, `bg-warning/10`,
`border-info/30`…). `lib/tone.ts` maps a meaning (`success`, `warning`, `error`, `info`,
`neutral`) to the class sets the UI needs: `text`, `chip`, `soft`, `dot` and `bar`. A place that
shows a state maps its own value to a tone: log levels
(`levelClass`), notification types, and health bars and chips on the home page. In progress is
`info` (blue) everywhere, including applications in transition; `warning` (amber) is for
warnings. A solid green or red button is a button variant (`success`, `destructive`), not a
class of its own. New states and new places use a tone; no `text-green-600`, `bg-red-500`,
`text-gray-500` and the like.

## State Indicators

`components/StateIndicator.vue` draws a state with its own icon shape as well as a colour,
so colour is never the only cue: `idle`, `busy`, `ok`, `warning` or `error`, each with a label
that is also its accessible name. It is an icon, an icon with the label in a badge
(`show-label`), or a small dot (`dot`, sized and placed by the caller). A place that has its
own states maps them to one of these in the app store (`sectionIndicator` for a settings
section, `workersIndicator` for the Workers menu item, `applicationIndicator` for an
application or container, `workerIndicator`, `domainIndicator`, `taskIndicator`). Used by the
settings badge and sidebar, the Workers menu item, the application, container, worker and
task statuses, a domain's sync state and the worker dot in a container card. A title with a
state beside it is a `SectionTitle` with the indicator in its slot.

## Dates

Every date shown goes through `formatDate` or `formatDateStringAgo` (`lib/utils.ts`), never
raw. Both parse with `parseDate`: backend timestamps without a timezone are UTC; ones with
`Z` or an offset are read as given.

## Settings Pages

Every settings page is a `SettingsSection.vue` given a `fields` list (`key`, `label`, and optionally `description`, `placeholder`, `type`, `secret`, `readonly`, `value`). A `readonly` field shows its `value` and is never loaded or saved. Tailscale and Cloudflare are env vars, so all their fields are read-only: the section then shows no Save / Test / Reset, and the page adds its dashboard links (`links` slot: the manager's machine in the Tailscale console; the tunnel and DNS records in Cloudflare) and its resync actions (`after` slot). S3 and notifications have editable fields: for those the section draws the inputs, loads, saves, tests and resets the values, shows a failing field's reason from the status under it, and handles the unsaved-changes dialog and the route-leave guard. Errors show inline; successes as toasts. The sidebar
and the pages read each section's status from the app store (`info.settings`). States are
drawn by `components/StateIndicator.vue` (see State Indicators).

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
