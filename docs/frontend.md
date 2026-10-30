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

Initial app boot fetches `GET /api` through the Pinia store in `ui/src/stores/app.ts`. That store owns the manager's status and follows its `info` topic: the version, the queue state (the pause banner and the restore overlay) and the settings status (the sidebar indicators and the settings pages). It also follows the `workers` topic (`workers`, null until loaded, and `workersError` if the first load fails) without holding the app back: the sidebar count (`workersIndicator`), the Workers page, the worker dot of an application's container cards (`workerStatus(hostname)`) and the Add Container dialog (the workers without a container of the application) all read that one list. The manager pushes each change (see Pushed Updates below). After an action the page updates the store at once (`setState` for pause and resume, `setSettingApplying` after a save, or `refresh()` after a save that empties a section, since nothing checks an empty one) and the next push confirms it, as with `applicationDeployStatus`, which the deploy, stop, backup and restore buttons set together with the statuses of the containers they touch (`containerStatus` for a card), and which the application header shows; deleting a container sets only that container (`updateContainerStatus`); `refresh()` fetches right away. A part the manager couldn't build comes as `null` and the store keeps the last value. The metrics views poll while they are open; the log views follow feeds (below).

## Pushed Updates

`ui/src/lib/updates.ts` holds the one Socket.IO connection (`/api/socket.io`) and the topics followed over it. A page or store follows a state topic with `watchState(topic, load, onState, onNotFound?)` or a feed with `watchFeed(topic, load, onRows)`, where `load` is the page's own REST call, and calls the returned `stop()` when it goes away; `first` settles with the first load, or rejects if it fails, so a page has its data without the socket. `onNotFound` is called when the manager answers 404 instead of a state, pushed or to a load after the first: the resource is gone. The manager sends nothing when a topic is followed, only each change after that, so the library loads again every time the socket connects (the first time and after every reconnect), once the manager has acknowledged the subscription: what changed before that is in the load. A state loaded before a newer one was pushed is dropped; a feed's rows are not, since they are merged. Reconnecting, and the fall back from WebSocket to long-polling, are Socket.IO's, and the UI has no polling of its own. The connection opens with the first watcher and closes when the last one stops. Topics so far, with what follows them:
- `info`: the app store (loading `GET /api`).
- `workers`: the app store (loading `GET /workers`): for each worker its status, ip, path, container count and newest minute of metrics. The Workers page, the sidebar, the container cards and the Add Container dialog read the store; each card on the Workers page shows the worker's minute from it, so the page asks for no metrics of its own. The graph pages (a worker's and an application's metrics, the Home page) still poll every 60 s.
- `worker:<hostname>`: the Worker page (loading `GET /workers/{hostname}`): the worker with its containers, its stack and its status. The page has no timer; an action on a container reloads it at once through `loadWorkerStatus`. A worker removed while the page is open shows as not found.
- `application:<project>/<name>`: the Application page (loading the application route). Each push replaces the whole application (volumes, domains and variables changed elsewhere too) and puts its status over the one an action set; an edit dialog keeps the values it copied when it opened. An action on the page reloads it at once through `loadApplication`. An application deleted while the page is open shows as not found.
- `tasks`: the Tasks page, the Upgrade page (which tasks it waits for before a stop) and the restore overlay, which follows it only while restoring (loading `GET /tasks`).
- `task-history`, a feed: the Tasks page's finished tasks (`GET /tasks/history`), a list (below).
  - Filtered by name, executor and status (dropdowns from `GET /tasks/history/filters`), a scope (its children too, `*` for any characters) and a From/To window the task was active in.
  - The newest 100 that match at first, newest finished first, and the list grows with the tasks pushed, up to 1000; the title counts all that match, and a note says when not all are shown. There is no paging.
  - The tables show when each task was queued, started and finished.
- `backup-status`: the Create Backup and Discover Backups buttons, each mirroring its own flag (loading `GET /backups/platform_backup_status`).
- `backups`: the Backups page (loading `GET /backups`), newest first; each pushed list replaces the shown one. A delete on the page loads the list again at once.
- `release`: the Upgrade page (loading `GET /release`). A check for updates is over when a release with a new `fetched_at` is pushed, or after 60 s with an error.
- `task-logs:<task>` and `task-logs:<task>/<part>`, feeds: the task log dialogs (`components/TaskLogs.vue`), a task's on the Tasks page and a container's part from its Event Logs; a list (below), of every line, oldest first, with no ceiling (a dialog has no filters).
  - The Tasks page's dialog reads the task's parts (`GET /tasks/{task_id}/parts`) when it opens: a select shows all the lines or one part's, and all the lines carry a Part column (the worker, or the part's id when its event is gone).
- `application-logs:<project>-<app>`, a feed: the application logs (`components/LogViewer.vue`); a list (below), of the newest 1000 lines that match, oldest first, up to 1000.
  - Filtered by the worker, the search and the From/To window (From starts five minutes back), applied together.
- `requests`, a feed: the Requests page; a list (below), of the newest 1000 requests that match, newest first, up to 1000.
- `notifications`, a feed: the header's list, whose `load` asks for the newest 20 while it has none and for the rows after its newest `created_at` otherwise, so it also recovers what it missed; it merges rows by id, newest first, so a row it has is not shown twice and one that arrives out of order lands in place. The bell's marker is on while the newest id is above the last one seen, which is kept in `localStorage` when the bell is opened.

The four lists follow one contract, `useFeedList` (`lib/feedList.ts`):
- The first load is what the route returns for the filters (the newest that match, as many as the list asks).
- A pushed row is kept when the filters let it through, by the `matches` beside the list's query type in `services/api/` (the dialogs keep every line), merged by key in the list's order, each once.
- Each time the socket connects, what the route returns given the newest row held is merged in: the rows after it (`after_id`), or for the finished tasks the newest again.
- A change of filter starts the list again with a fresh load, and a load asked for filters since changed is dropped.
- A list with a ceiling keeps its newest rows; a note says when the route had more or the oldest were dropped, and its tooltip that only the latest are shown and how to see older ones.

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

`ui/src/lib/api.ts` holds `request(method, path, { query, body })`, the only place the UI calls `fetch` for the API: it waits `LOAD_DELAY` (see the development guide), builds the URL under `/api` with the query (leaving out null, undefined and empty values), sends the body as JSON and returns the answer's JSON. A failed answer throws an `APIError` with the manager's `detail` as its message (or which call failed, when there is none) and its `status` and `response`; it logs nothing, the caller does.

`ui/src/services/api/` has a named, typed function for every call, each a line over `request` (`fetchApplication(project, name)`, `deployApplication(project, name)`, `deleteContainer(project, application, hostname, force)`, …), even one that a single component uses, in a module for each resource (`applications.ts`, `tasks.ts`, …) with the shapes it answers with. Everything imports from `@/services/api` (its `index.ts` re-exports every module), never a module, so that a test's mock of it reaches every caller; `_common.ts` holds what the modules share and is not exported. Components import these functions and pass the names of what they act on; they never write a method or a path, and no API client is passed down as a prop. A list's query type has its `matches` beside it (`matchesLogQuery`, `matchesRequestQuery`, `matchesTaskHistoryQuery`): whether its route lets a row through the query, which is what a view keeps of the rows pushed to it. `ui/src/lib/metrics.ts` shapes what the metrics calls return.

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
