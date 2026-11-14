---
name: sage-engine-review
description: Independently check Sage's core operation engine and the code that uses it - that every task's scopes cover the resources it writes, that nothing can deadlock or race on those resources, and that the facts tasks and decisions read flow one way from their owners. Use when a change adds or alters a task, a scope, a remote write, a Fact, a lock or a decision table, when asked to evaluate the engine, or to check a planned design for it.
---

# Sage Engine Review

Keep the core scheduler engine intact as Sage grows: catch code that drifts from the resource scope locks, and
reconcile the tasks' data flow (observation sources, decision tables, the consumers of facts) so the resources they
touch stay free of deadlocks and races. The rules are app/AGENTS's [Operation Queue And
Scheduler](../../../app/AGENTS.md#operation-queue-and-scheduler), [Services, Memory And
Updates](../../../app/AGENTS.md#services-memory-and-updates), [Code Layout](../../../app/AGENTS.md#code-layout) and
[Async And `task_id`](../../../app/AGENTS.md#async-and-task_id), with the root's [working
rules](../../../AGENTS.md#working-rules) 3 and 9–11; this skill is how to check the code against them. A review
records findings; fixing one is a separate change the user approves.

## Scope

- In: the engine (`utils/queue.py`, `utils/executor.py`, `services/manager/queue.py`, `scheduler.py`, `Fact` in
  `services/updates.py`) and its boundary: every enqueue, every remote write and the tasks that reach it, every reader
  of the queue, every Fact and lock, every decision table and its consumers, and what a task body freezes, re-reads
  and certifies.
- Out: hunting bugs or auditing the rest of the code, the UI and payloads, docs drift, a resource's lifecycle rules
  beyond its locks, how long a task waits, and scaling: a cost that shows only beyond the envelope in [Core
  Context](../../../AGENTS.md#core-context-to-preserve) is not a finding. Something out of scope met on the way gets
  one line in the hand-off, not an investigation.
- Say the subject (the engine, a change against its baseline commit, or a planned design) and the commit at the start.
  A planned design's open choices stay open.

## Ground it

- Initialize the memory repo through [root AGENTS](../../../AGENTS.md#agent-memory): read its global rules and
  memory index, skipping review and todo/backlog content.
- Every run is independent. Do not read, search, reuse or cite prior reviews, their inventories or probes, or
  todo/backlog items. Do not use earlier review conclusions already in context as a checklist or evidence. Build
  the inventory, probes and findings afresh from the source being reviewed.
- Read the engine's files in full, the rules above, and the docs on the queue: backend.md's [Operation
  Queue](../../../docs/backend.md#operation-queue), [Pause And Stop](../../../docs/backend.md#pause-and-stop),
  [Execution Modes And Pools](../../../docs/backend.md#execution-modes-and-pools), [Choosing A Task
  Shape](../../../docs/backend.md#choosing-a-task-shape) and [Pushed Updates](../../../docs/backend.md#pushed-updates),
  and operations.md's [Scopes and admission](../../../docs/operations.md#scopes-and-admission) and [Task
  catalog](../../../docs/operations.md#task-catalog). They are claims to check against the source, not proof.
- Inspect the working tree, staged and unstaged, without changing it; read an older version with
  `git show <rev>:<path>`.

## Build the inventory

From the repository root, `python3 -I .agents/skills/sage-engine-review/scripts/engine_inventory.py app` lists, from
the source:

- **enqueues:** each `add_task` with its key, name, scopes (what a variable in them holds), policy, lane, quiet flag,
  task body, params and inputs, and each call of a wrapper that enqueues the task it is given, with that call;
- **effects:** each remote call (SSH, Cloudflare, S3) by enclosing function, with every task body that reaches it
  through inline calls and that task's scopes and policy, or the root it is reached from when no task is;
- **locks:** each block that holds a lock or a database transaction and what it calls, marking a request, a Fact
  check or a publication;
- **readers:** what reads the queue and the params it reads;
- **facts:** each Fact, what its callback calls, and where Facts are checked;
- **decisions:** each decision in a `decisions.py` and its users, and each owner's raw fact compared in a workflow or
  a route, called on its owner or read off a value that holds it.

Calls are resolved by name, so a list can name more than really reaches a line: read the source behind every line
before citing it, and complete what a name cannot show (a value passed in, a writer reached through a callback). For
a change, run it on the baseline too (`git archive <rev> app | tar -x -C <scratch dir>`) and compare.

Cross-check the candidate list against provider implementations and their route, startup, callback and thread
entry points: synchronous provider calls made inline can be absent, including an explicit test's effects. Inspect
explicit `.acquire()` / `.release()` paths too; the lock list covers context-manager blocks. Inside a provider class
its own API calls are not listed as remote, so read its lock blocks: a lock held across one makes every reader of that
lock wait on the provider, the event loop included.

## Answer these, each with source locations

1. **Races on the resources tasks touch.** For each remote write, the resource it changes (from its path or its
   owner's writer), every task that reaches it and that task's scopes. On one lock path, each writer holds the
   resource's scope or an ancestor; under two roots, each writer holds both sides or only removes. A write no task
   reaches (a route, a callback, a thread) is a finding. Each scope is built by its resource's package; one built from
   a binding that can change is checked again at the task's start.
2. **Deadlock.** The wait graph over scopes, the queue lock, Fact locks, service locks, lanes, the event loop and
   SQLite: for each lock, what runs under it and on which threads. Name the edges the engine makes impossible (the
   code) and those a rule forbids (the rule), and the call sites that keep or break each: a Fact callback that checks
   a Fact, an owner that enqueues or publishes under its lock, a sync task that waits on its own lane, a body that
   waits for another queued task.
3. **Consistency over time.** Enqueue, start, result: what a task freezes in `inputs`, what it reads again at its
   start, how its result is tied to what it applied (a fingerprint, a generation, its authority), and what guards it
   against writers outside the queue. Each reference a task carries in its key, params or inputs resolves at its start
   to the resource its scopes were built for: an id a deletion frees for reuse, or a row field another writer can
   change, is a binding to check again. Each DEDUP key has its periodic pass; a change that must be seen after a running
   task uses REPLACE. Order between tasks comes from scopes or from a body's follow-up request; nothing relies on when
   a task starts or on the order of tasks that do not conflict, and nothing starts a task ahead of earlier conflicting
   work. An owner's memory that tasks sharing no scope write (one replacing it whole with what it gathered across an
   await) is traced for lost updates, and a Fact callback for the requests lost when it raises after `last` is set. A
   request function that writes rows before its `add_task` leaves a moment with neither; a body that reads the queue
   and then those rows is traced across it.
4. **Data flow.** Each reader of the queue finds a subject by key and a resource by scope, never identity in params;
   the queue's facts only publish, and work is requested by a body's end, a state Fact's callback or a scheduled pass.
   For each decision table: its inputs with their owner, source and knowledge states, the bound on each state not
   known yet (root working rule 10), and its consumers. A consumer that composes a decided question again from raw
   facts is a finding.

## Judge with evidence

- "By construction" names the code that makes a violation impossible; "by rule" names the AGENTS rule and whether the
  inventory shows every call site keeping it. Do not hold code to a rule the AGENTS files lack; a gap in the rules is
  itself a finding.
- A race needs the reachable interleaving and why the actual protection (scope, transaction, fingerprint,
  generation) does not stop it. Reachable means through an entry point the subject has (a route, startup, a scheduled
  job, a Fact callback, a body's follow-up request, a thread) and past its guards: the resource kinds, filters and
  preconditions it admits. A probe that calls a lower-level writer directly shows the mechanism, not the
  interleaving, and does not count as reproduced. A deadlock needs the whole wait cycle. Accepted lag is not a bug.
- A comparison with an outside design (a workqueue, a lock manager) is checked against its source or docs and
  linked.
- Settle a doubt on the real `TaskQueue`, `Fact` or `next_to_start` in the session's scratch space, task bodies
  mocked, extending the scenario suites' model where it helps; plant a violation to see what catches it. Never
  contact a live provider or write to the live database. Run the suites without writing into the tree:
  `PYTHONDONTWRITEBYTECODE=1 pytest --no-cov -p no:cacheprovider` from `app/`.
- Grade each finding: source-proven, reproduced or live-verified. A passing suite or a bounded review does not prove
  the absence of races.

## Record and hand off

- Save each run's report in a new directory in the memory repo, with run-local numbered findings and a detail
  file for each: locations, the interleaving or wait cycle, the rule it breaks, a direction for the fix, the evidence
  and its limits. Do not read or update existing tracker items, deduplicate against them or reconcile the report
  with earlier runs.
- Report by the four questions, races and deadlocks first, then what was not verified.
- Change no runtime code, test or doc during the review.

## Refine this skill

- When a review shows a blind spot, a false positive or an unclear step, make the smallest reusable fix in the
  procedure or the script. A rule goes to the AGENTS file that owns it, with the user's approval. Hard-code no task
  names, scope roots, counts or answers to past findings.
- Keep `.agents/skills/sage-engine-review/` and `.claude/skills/sage-engine-review/` identical (SKILL.md and the
  script), validate the frontmatter and links, run the script, and report what changed and why.
