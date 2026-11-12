---
name: sage-review
description: Review Sage's code against its rules and the behavior that was asked for, covering ownership, lifecycle contracts, queue admission and scopes, races, deadlocks, the API and UI contracts, and drift in the docs. Use for an operational audit, for a sprint's or a branch's closing review (what it changed, against its goals and a baseline), or to check a planned design when asked.
---

# Sage Review

Find concrete gaps between what was asked, Sage's rules and what the code does. The rules live in the AGENTS files
([root](../../../AGENTS.md), [app](../../../app/AGENTS.md), [ui](../../../ui/AGENTS.md)); this skill is how to check
them. A review records findings; fixing one is a separate change the user approves.

## Choose the mode

- **Audit:** the current code, or the part of it the user names.
- **Closing review:** a sprint or a branch against its goals and a baseline commit, with an audit of what it touched.
- **Planned design:** a backlog design, only when the user asks. Its open choices stay open, and a requirement of a
  future feature is not held against current code.

Say the mode, the subject and the baseline (commit or branch) at the start.

## Ground it

- Initialize the memory repo through the root AGENTS (Agent Memory). For an audit, read the tracker and the detail
  files of the items in scope. For a closing review, read the sprint's plan in the backlog and the settled decisions
  in its items, but skip the tracker's findings and the items' earlier review sections: they are read only after the
  review's own pass, to reconcile (Record and hand off), so that known answers do not steer it.
- Read the README, the three AGENTS files and the docs the subject touches
  ([architecture](../../../docs/architecture.md), [backend](../../../docs/backend.md),
  [operations](../../../docs/operations.md), [frontend](../../../docs/frontend.md)). Docs, comments, tests and the
  task catalog are claims to check against the source, not proof.
- Inspect the working tree, staged and unstaged, without changing it; read an older version with
  `git show <rev>:<path>`. Read the source again before citing a location.

## Trace the subject

- Each operation in scope, in its order: trigger, request function, admission (key and policy), scopes and lane,
  pre-checks, recorded intent, effects, verified outcome, publication, and its failure and restart recovery.
- Each resource or model in scope, the other way: every writer (commands, task bodies, startup and restore, ingestion,
  callbacks, FK actions, threads) and every reader (payload readers, topics, pages). Queue scopes do not protect a
  write outside the queue. A model or field with writers and no reader, or a reader and no writer, is a finding.
- The process's start, a restore's end and a resource's deletion, as sequences across owners: each owner's memory
  empty or one observation old, and what each publication and cleanup does with a fact not observed yet.
- The [Task Catalog](../../../docs/operations.md#task-catalog) and the model writer index against what the code
  enqueues and writes; investigate a disagreement in either direction.
- The read model through to the UI: the REST payload, what it carries and what makes it grow, the topic and its
  sources, the page's state and the order of its loads and pushes, each control and the admission of its request.
  Follow a value shown to people (a reason, a detail, a label) from where it is written to the text that shows it.
  Two layers each tested green can still promise different things: check the contract across them.

## Judge with evidence

- Name the rule each finding breaks, by its owner: the root's working rules, app/AGENTS (Code Layout, Database And
  Commands, the resource contracts, Operation Queue And Scheduler, Async And `task_id`, Testing) or ui/AGENTS. Do not
  restate a rule here, and do not hold the code to one the AGENTS files lack; a gap in the rules is itself a finding.
- Walk the rules the other way too: for each contract bullet of a resource in scope, point to the code that meets it;
  one that nothing meets is a finding.
- A race needs the reachable interleaving and why the actual protection (scope, transaction, lock, fingerprint,
  generation) does not stop it. A deadlock needs the whole wait cycle (scopes, locks, Fact callbacks, executor slots,
  the event loop). Check progress as well as exclusion; a lane's size is not a lock.
- An optimization names the redundant work or needless exclusion, the simplest change that keeps the behavior, and
  its cost in state, threads, locks and requests. A speed, size or capacity claim is measured or labelled unmeasured.
  Accepted lag and opportunistic dispatch are not bugs.
- Use focused existing tests or scratch reproductions, in the session's scratch space, when they settle a doubt: real
  local state and Sage's own decisions, with provider effects replaced. Never contact a live provider or write to the
  live database. A reproduction that fails stays failing.
- Grade each finding: source-proven, reproduced or live-verified.

## Drift check

- List the relative links and the backticked code names and paths in the docs and the AGENTS files, and check each
  against the source (a short scratch script does it). A name gone from the code is drift.
- Then read the sections the subject touches for claims no name shows: a signal, a scope, a status column, a route.
  Check the comments and docstrings of the files touched for references to code that is gone.

## Closing review

Besides the audit of what the sprint touched, answer in this shape, each claim with a source location or an example:

1. **Goals:** a table of each goal, its result and what remains.
2. **Behavior against the baseline:** by area (containers, workers, Domains, backups and restore, settings, the UI),
   what Sage now does differently, and each consequence a user meets with where it is stated (UI text, README, docs).
   A consequence stated nowhere is a finding.
3. **Responsiveness:** the structural changes that make an action return sooner or later, apart from what was
   measured; no blanket faster or slower.
4. **Testability:** what can now be tested with real local state and provider effects replaced, and which contracts
   between layers no test crosses.
5. **Design:** what the sprint could have done better, and backend and UI patterns worth adopting, each concrete and
   with what it would change; say what not to add.
6. **Rules and docs:** drift found, and the rules the code does not meet.

File and line counts are context, never the measure.

## Record and hand off

- A closing review reconciles once its own pass is done: read the open findings and the earlier review sections, then
  say which the pass found itself, which it missed (and which step should have caught each, for Refine this skill)
  and which no longer hold against the source.
- Follow the memory repo's tracker rules. A new finding is a new numbered item with its detail file: locations,
  initial state, trigger or interleaving, wrong outcome, the rule or requirement, a direction for the fix, the
  evidence and its limits. Update a matching open item instead of adding another, and say which known findings still
  hold against the current source. A closing review goes in a dated section of the sprint's item.
- Report correctness findings by impact, then optimizations, then drift, then coverage gaps and what was not
  verified. A passing suite or a bounded audit does not prove the absence of races.
- Change no runtime code, test or doc during the review.

## Refine this skill

- When a review shows a blind spot, a false positive or an unclear step, make the smallest reusable fix here, in the
  procedure. A rule goes to the AGENTS file that owns it, with the user's approval. Hard-code no task names, scope
  roots, counts or answers to past findings.
- Keep `.agents/skills/sage-review/SKILL.md` and `.claude/skills/sage-review/SKILL.md` identical, validate the
  frontmatter and links, and report what changed and why.
