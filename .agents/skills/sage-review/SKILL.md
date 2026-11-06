---
name: sage-review
description: Review Sage's operational engine and backlog designs for model consistency, scope and admission errors, deadlocks, races, and useful optimizations. Use for operational correctness audits, including reviews after a sprint.
---

# Sage Review

Review the full current operational engine and its planned changes. Produce evidence-backed
bugs, rule violations, optimizations and backlog design notes. A review may update the memory
repo's findings and design notes and refine this skill; runtime fixes are a separate task.

## Ground the review

- Follow the root [AGENTS.md](../../../AGENTS.md) to locate and initialize the memory repo.
  Read its `sage/todos/todo.md`, `sage/todos/BACKLOG.md` and relevant linked detail files.
  Review every open planned item for engine impact, reading its detail when it has one;
  archives are context for a specific decision, not an additional audit target.
- Read [README.md](../../../README.md), [app/AGENTS.md](../../../app/AGENTS.md),
  [architecture](../../../docs/architecture.md), [backend](../../../docs/backend.md) and
  [scheduler/data-write reference](../../../docs/operations.md). The AGENTS files own
  operational rules; this skill owns how to check them. Do not move an always-on invariant
  into a skill that only runs during audits.
- Inspect the current working tree, including staged and unstaged changes. A diff helps
  prioritize attention; it does not replace reviewing unchanged interacting code. Preserve
  the user's staged work. Re-read source before reporting a location or behavior.
- Treat docs, comments, tests and backlog descriptions as claims to compare with source.
  Establish what executes now. A name, loop or stale comment is not proof of task granularity,
  remote collection, scope ownership or serialized execution.

## Trace operations and all writers

Start at `app/scheduler.py`, `app/utils/queue.py`, `app/utils/executor.py`,
`app/services/manager/`, `app/routes/`, `app/services/db/` and `app/main.py`.
Follow service, report, metrics, logs and template code as the paths require.

Build a compact working inventory of triggers, target identity, actual held scopes, lane,
admission/quiet/restore flags, reads, writes, external effects and follow-ups. Compare it
with the task catalog; investigate missing entries and disagreement in either direction.
Do not assume the catalog enumerates every writer.

Include direct CRUD, request helpers that write before enqueueing, startup and restore,
model save overrides, signals and FK actions, bulk SQL, ingestion, daemon threads,
terminal hooks, notifications and caches. Trace both operation → writes and model →
writers. Queue scopes do not protect a direct route write or an unrelated pool.

For each operation, follow actual execution order through validation, snapshots, status
changes, remote effects, result publication and failure/finally paths. Check in particular:

- Preconditions checked at dispatch as well as at the route, and ID-based re-fetches when
  a target can disappear. Separate missing-before-start no-ops from mid-operation failures
  that must report partial work. Follow required related rows too.
- Desired config, last-known runtime state, operation/busy status and applied proof as
  distinct facts. Check whether the operation's own status writes disable a later step,
  invalidate a report, or make a runtime-restoration decision use the wrong state.
- Snapshots that remain coherent across awaits, dirty-field saves and concurrent edits,
  bindings/placements that change while queued, and guarded completion writes after I/O.
  A stale success must not certify newer desired state or overwrite a later failure.
- Implicit counters, cascades and unlinks; partial success, repeated attempts, orphan
  cleanup and offline/rejoining workers; and each writer's UI publication/cache refresh.
  Verify ordering between materializing artifacts, publishing them and recording proof.

## Check concurrency and progress

Apply the current rules in [app/AGENTS.md](../../../app/AGENTS.md), especially Operation
Queue And Scheduler, Async Boundaries, Services and Database. Check the implementation
of those rules too; passing tests alone does not establish them.

- **Exclusion and identity:** derive the smallest sufficient scope set from shared state
  and effects, then inspect every enqueue source, restore follow-up and direct/inline
  call. Inline calls inherit the caller's scopes. Scopes are hierarchical and independent
  of lanes; a pool's thread count is not a lock. Check linked-entity scope changes,
  broad barriers and the rule that a task with both `platform` and `app` also holds `domain`.
  Preserve worker convergence's platform-only boundary and the absence of per-worker
  scopes; report a concrete violation rather than broadening every task for safety.
- **Admission and follow-ups:** verify name + exact scopes as duplicate identity, including
  parameter-distinct actions, source-dependent DEDUP/REPLACE/QUEUE and rejected admission.
  Check requests arriving after an in-flight read, discarded pending work, retained latest
  desired state, and parent/child repair loops. Enqueueing a dependent task is not awaiting
  its completion. A parent must release conflicting scopes before its child can run.
- **Deadlocks:** trace a concrete wait cycle through scopes, service/queue/Fact locks,
  callbacks, event-loop work and executor slots. Check re-entry when a change notification
  enqueues work, lock order, and a sync parent occupying the pool its children need.
  Async fan-out parents must leave pool threads available. Include off-queue writers and
  callbacks; do not infer safety from different task scopes alone.
- **Races:** show the reachable interleaving and explain why the actual scopes, transaction,
  lock, snapshot or generation check does not prevent it. Cover edit/delete/relink during
  queued and running work, stale observations/results, cache replacement during reads,
  and cancellation while offloaded work can still finish.
- **Execution boundaries:** ordinary local Peewee/SQLite queries and writes stay inline
  and synchronous under WAL; do not recommend async SQL wrappers or thread hops for them.
  Heavy bulk/file/backup/encryption work is the documented offload exception. Blocking
  network/remote work uses the existing context-preserving lane helpers; verify ambient
  executor selection and `task_id` propagation. Inspect transactions/locks held across
  waits for a demonstrated contention or progress problem.
- **Lifecycle and observation:** check pause, resume, restoring, draining, restart recovery
  and pending-work replacement; a running operation must not lose its protection while
  child work remains. Follow busy-state ownership, report freshness/version checks,
  unknown versus offline, retained valid proof on failure, and restored caches/services.
  Check progress for unrelated applications and for metric/log maintenance, not only
  scope mutual exclusion.

Respect documented trade-offs: infrequent changes, tolerated reconciliation lag and
opportunistic dispatch do not by themselves establish bugs. Deliberate new feature
semantics in an agreed design are not violations merely because current behavior differs.

## Evaluate useful optimizations

Look for work whose ownership, batching, polling or exclusion no longer matches its data
source: independent entities serialized unnecessarily, repeated scans/publications,
redundant state, obsolete remote work, excessive recurring provider calls, and lane use
that stalls unrelated work. Follow the enqueue boundary and handler to establish the
actual unit of work before recommending per-entity fan-out.

For each suggestion, identify the concrete cost and the simplest change that preserves
behavior, with its added state/locks/threads/requests and effect on correctness. Use
measurements when claiming a speed or capacity improvement; otherwise label the benefit
as a source-based estimate or a measurement needed. Prefer an existing tick, owner or
stock mechanism. Exclude cosmetic cleanup and speculative machinery with no clear gain.

## Review backlog designs

For each open design affecting the engine, apply the same ownership, write, exclusion,
admission, progress and recovery checks to its proposed final behavior and dependencies.
Identify which interfaces or guarantees the plan must preserve; do not invent its
implementation or treat intentionally undecided details as implemented bugs.

Where a design conflicts with an invariant, add a concise **Engine review** note under
the owning backlog item or detail-file design section: the conflicting proposal, the
named rule/source, the failing scenario, and the minimum design constraint that resolves
it. Identify open choices as open. Preserve the feature's intended behavior, settled
decisions, status and work order. A proposed change to a settled decision stays explicit
in the note. Update an existing matching note instead of accumulating duplicate warnings.

## Record and report

Use the memory repo's todo conventions. Add new code findings as numbered items, and
update matching existing items with fresh evidence instead of duplicating them. Recheck
known findings against current source; do not repeat fixed bugs or call a planned remedy
an implemented fix. Keep backlog design notes with their owner and link them in the report.

Present findings in these groups, with correctness items ordered by impact:

- **Bugs and violations:** title/priority, exact source locations, trigger and initial
  state, execution sequence or interleaving, incorrect outcome, broken invariant, minimal
  correction direction, evidence and tracker link. A deadlock needs the wait cycle; a
  race needs the competing paths. A rule violation names its rule even when no failure
  is reproduced. Distinguish source-proven findings from reproduced ones.
- **Optimizations:** location, concrete unnecessary cost, proposed simplification,
  benefit/cost and preserved guarantees; mark any unmeasured performance claim.
- **Backlog notes:** affected item, design conflict or compatibility constraint, and
  where the note was added or updated.

State meaningful coverage gaps and unresolved assumptions separately. Omit unsupported
suspicions from the confirmed list; report no findings plainly when appropriate. Do not
claim the engine is proven race/deadlock free from a bounded audit.

Use focused existing tests or scratch reproductions when they resolve uncertainty, with
external services stubbed and real local decision/state code. Follow the project's test
rules: keep discovered failures visible and do not mutate the live database, contact live
providers, repair production code or implement backlog work as part of the review.
Report what was inspected, what ran and what remains unverified.


## Evolve this skill

You may refine this skill during a review as session progress, demonstrated findings and
user feedback expose blind spots, false positives or unclear instructions. Apply the
smallest reusable lesson: improve an existing check, remove an obsolete assumption or add
a missing review criterion. Keep specific bugs and session history in the tracker.

Preserve the evidence standard, review scope and rules owned by AGENTS files. Do not
weaken a check to dismiss a finding or turn one incident into a blanket restriction.
Update `.agents/skills/sage-review/SKILL.md` and its identical
`.claude/skills/sage-review/SKILL.md` copy together. Validate the skill and its links,
confirm both copies match, and briefly report what changed and why with the review results.
