# AGENTS — ui (Vue frontend)

Rules for the Vue frontend. Read the root [../AGENTS.md](../AGENTS.md) first for the architecture and repo-wide rules; this file is UI-specific.

- Build from the existing component library (shadcn-vue under `ui/src/components/ui`, add more of its components as needed) rather than hand-rolled HTML and CSS. Don't add a new UI framework or library for one small component.
- UI text is written for people: a short summary of what will happen or what a state means, not commands, file names or protocol details (those belong in the docs).
- A refusal says what isn't allowed, plainly: no consequences and no remedies.
- Forms: a placeholder is only ever an example, written "e.g. …"; a field with a default is prefilled with it; optional fields say "(optional)" in the label.
- Domain CORS origins use blank as an active default: leave the field blank and describe the current own origin. An explicit origin is a manually maintained value, so never substitute the computed default into the stored field or rewrite another Domain's references on a rename.
- Dates are always shown through `formatDate` / `formatDateStringAgo` (`ui/src/lib/utils.ts`), never raw.
- The manager's status (queue state, worker summary, settings status) lives in the app store, which follows the `info` topic; pages follow their own topics with `watchState(topic, load, onState)` or `watchFeed(topic, load, onRows)` (`ui/src/lib/updates.ts`, Socket.IO; `load` is the page's REST call), with no polling of their own for them. After an action, update the store at once and let the next push confirm.
- Every action that returns before the work is done (a queued task) shows its in-progress state at once wherever the server has such a state (application and container status, a settings section applying, the queue state); a new queued action gets the same. A control that shows a spinner and stays disabled while its request runs (a confirmation dialog's button, a `submitting` flag) already does this for its own action; an app-store status is set before the request, and undone if it fails, only when the state shows on other parts of the page too.
- State colours come from tones (`lib/tone.ts`) and `StateIndicator`, never palette classes (see [docs/frontend.md](../docs/frontend.md)).
- A refresh or timer stays a single chain when calls overlap (timer, focus, manual).
- Every call to the manager's API is a named, typed function in `ui/src/services/api/`, even when one component uses it, which overrides the global rule against single-use helpers. Components import them from `@/services/api` and never write a method, a path or `request(...)`; see API Layer in [docs/frontend.md](../docs/frontend.md).
