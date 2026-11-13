"""The engine's inventory, read from Sage's source: what the sage-engine-review skill checks.

Usage, from the repository root:
  python3 -I .agents/skills/sage-engine-review/scripts/engine_inventory.py app [section ...]
Sections: enqueues, effects, locks, readers, facts, decisions (all when none is named).

Calls are resolved by name: `self.<method>` inside the Manager's mixins (any mixin's method), `<Class>().<method>`,
plain and module functions, and a function handed to an offload helper. A list of the tasks that reach a line can
therefore name more than really do: read the source behind a line before citing it, and complete what a name cannot
show (a scope held in a variable, a writer reached through a value passed in).
"""
import ast
import pathlib
import sys
from collections import defaultdict

# The service classes that reach outside the manager (SSH to workers, Cloudflare, S3). A call on one, or on a variable
# that holds one, is remote when it is awaited, offloaded or one of the class's async methods; any other call on one
# reads what it holds in memory. Variables named here hold one by convention.
PROVIDERS = {"Tailscale", "Cloudflare", "S3", "S3Session"}
PROVIDER_VARIABLES = {"cloudflare": "Cloudflare", "session": "S3Session"}
# The offload helpers, with the position of the function they run.
OFFLOADS = {"run_in_executor_with_context": 0, "submit_with_context": 1}
ENQUEUES = ("add_task", "add_restore", "request_restore")
QUEUE_READS = {"task_snapshot", "has_task", "cancel_tasks", "is_busy"}
# The last name of a call that requests work, checks a Fact or publishes (besides any `request_*`).
SIGNALS = {"add_task", "add_restore", "check", "changed", "publish", "notify"}
SECTIONS = ("enqueues", "effects", "locks", "readers", "facts", "decisions")


def segment(source, node, width=110):
  text = " ".join((ast.get_source_segment(source, node) or "").split())
  return text if len(text) <= width else text[:width - 1] + "…"


def signals(name):
  last = name.rsplit(".", 1)[-1]
  return last in SIGNALS or last.startswith("request_")


def marked(names):
  return ", ".join(f"*{name}" if signals(name) else name for name in names) or "—"


class Module:
  def __init__(self, root, path):
    self.path = path.relative_to(root).as_posix()
    self.name = self.path[:-3]
    self.source = path.read_text()
    self.tree = ast.parse(self.source)
    self.manager = self.name.startswith("services/manager/")
    self.provider = any(isinstance(node, ast.ClassDef) and node.name in PROVIDERS for node in self.tree.body)


class Function:
  def __init__(self, module, node, owner=None, parent=None):
    self.module, self.node, self.owner, self.parent = module, node, owner, parent
    self.name = f"{parent.name}.<{node.name}>" if parent else f"{owner}.{node.name}" if owner else node.name
    self.parameters = [arg.arg for arg in node.args.args]
    self.calls = []           # (kind, name, class, call node, argument node or None)
    self.remote = []          # (provider, method, line, summary)
    self.enqueues = []        # (line, keywords)
    self.locks = []           # (line, lock, names called)
    self.queue_reads, self.params_read = set(), set()
    self.fact_checks, self.facts, self.comparisons = [], [], []

  @property
  def where(self):
    return f"{self.module.path}:{self.node.lineno}"


def own_nodes(function):
  """The nodes of the function's body, without those of the functions nested in it."""
  stack = list(function.node.body)
  while stack:
    node = stack.pop()
    yield node
    stack.extend(child for child in ast.iter_child_nodes(node)
                 if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)))


def provider_of(value, assigned):
  if isinstance(value, ast.Call) and isinstance(value.func, ast.Name) and value.func.id in PROVIDERS:
    return value.func.id
  if isinstance(value, ast.Call) and isinstance(value.func, ast.Attribute) and value.func.attr == "session":
    return "S3Session"
  if isinstance(value, ast.Name):
    return assigned.get(value.id) or PROVIDER_VARIABLES.get(value.id)
  return None


def scan(function, classes, async_methods):
  source, nodes = function.module.source, list(own_nodes(function))
  assigned = {node.targets[0].id: provider_of(node.value, {}) for node in nodes
              if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
              and provider_of(node.value, {})}
  awaited = {id(node.value) for node in nodes if isinstance(node, ast.Await)}
  for node in nodes:
    if isinstance(node, ast.Call):
      func = node.func
      name = func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else ""
      references = [(func, None)]
      if name in OFFLOADS and len(node.args) > OFFLOADS[name]:
        references.append((node.args[OFFLOADS[name]], node.args[OFFLOADS[name]]))
      for reference, argument in references:
        if isinstance(reference, ast.Attribute):
          value = reference.value
          if isinstance(value, ast.Name) and value.id == "self":
            function.calls.append(("self", reference.attr, function.owner, node, argument))
          elif isinstance(value, ast.Call) and isinstance(value.func, ast.Name) and value.func.id in classes:
            function.calls.append(("class", reference.attr, value.func.id, node, argument))
          elif isinstance(value, ast.Name):
            function.calls.append(("function", reference.attr, None, node, argument))
          provider = provider_of(value, assigned)
          if provider and not function.module.provider and (
                  argument is not None or id(node) in awaited or (provider, reference.attr) in async_methods):
            arguments = node.args[OFFLOADS[name] + 1:] if argument is not None else node.args
            summary = ", ".join(segment(source, item, 70) for item in arguments[:2])
            function.remote.append((provider, reference.attr, node.lineno, summary))
        elif isinstance(reference, ast.Name):
          function.calls.append(("function", reference.id, None, node, argument))
      if name in ENQUEUES and node.keywords and not any(keyword.arg is None for keyword in node.keywords):
        function.enqueues.append((node.lineno, {keyword.arg: keyword.value for keyword in node.keywords}))
      if name in QUEUE_READS:
        function.queue_reads.add(name)
      if name == "check" and isinstance(func, ast.Attribute) and not node.args and not node.keywords:
        function.fact_checks.append((node.lineno, segment(source, func.value, 50)))
      if name == "Fact":
        function.facts.append((node.lineno, node))
    elif isinstance(node, ast.Subscript) and isinstance(node.value, ast.Subscript):
      inner = node.value
      if isinstance(inner.slice, ast.Constant) and inner.slice.value == "params" and isinstance(node.slice, ast.Constant):
        function.params_read.add(node.slice.value)
    elif isinstance(node, (ast.With, ast.AsyncWith)):
      if any("lock" in segment(source, item.context_expr).lower() for item in node.items):
        called = sorted({segment(source, call.func, 60) for statement in node.body for call in ast.walk(statement)
                         if isinstance(call, ast.Call)})
        function.locks.append((node.lineno, segment(source, node.items[0].context_expr, 40), called))
    elif isinstance(node, ast.Compare) and function.module.name.startswith(("services/manager/", "routes/")):
      sides = [node.left, *node.comparators]
      owner_fact = any(isinstance(side, ast.Call) and isinstance(side.func, ast.Attribute)
                       and isinstance(side.func.value, ast.Call) and isinstance(side.func.value.func, ast.Name)
                       and side.func.value.func.id in classes for side in sides)
      if owner_fact and any(isinstance(side, ast.Constant) and isinstance(side.value, str) for side in sides):
        function.comparisons.append((node.lineno, segment(source, node, 90)))


def collect(root):
  modules = [Module(root, path) for path in sorted(root.rglob("*.py"))
             if not any(part in ("tests", "site-packages") or part.startswith(".") for part in path.relative_to(root).parts)]
  classes = {node.name for module in modules for node in module.tree.body if isinstance(node, ast.ClassDef)}
  async_methods = {(node.name, item.name) for module in modules if module.provider for node in module.tree.body
                   if isinstance(node, ast.ClassDef) and node.name in PROVIDERS
                   for item in node.body if isinstance(item, ast.AsyncFunctionDef)}
  functions = []

  def visit(module, body, owner=None, parent=None):
    for node in body:
      if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        function = Function(module, node, owner, parent)
        functions.append(function)
        visit(module, node.body, owner, function)
      elif isinstance(node, ast.ClassDef) and parent is None:
        visit(module, node.body, node.name)

  for module in modules:
    visit(module, module.tree.body)
  for function in functions:
    scan(function, classes, async_methods)
  return functions


def resolver(functions):
  by_method, by_function, manager_methods, nested = defaultdict(list), defaultdict(list), defaultdict(list), {}
  for function in functions:
    if function.parent is not None:
      nested[(function.parent, function.node.name)] = function
    elif function.owner:
      by_method[(function.owner, function.node.name)].append(function)
      if function.module.manager:
        manager_methods[function.node.name].append(function)
    else:
      by_function[function.node.name].append(function)

  def resolve(caller, kind, name, owner):
    if kind == "self":
      return manager_methods.get(name, []) if caller.module.manager else by_method.get((owner, name), [])
    if kind == "class":
      return manager_methods.get(name, []) if owner == "Manager" else by_method.get((owner, name), [])
    scope = caller
    while scope is not None:
      if (scope, name) in nested:
        return [nested[(scope, name)]]
      scope = scope.parent
    return by_function.get(name, [])
  return resolve


def explain(function, expr, call=None, caller=None, depth=2):
  """What the names in an enqueue's expression hold: a wrapper's argument from its call, the function's own assignment
  and what is added to it."""
  notes = []
  for name in sorted({node.id for node in ast.walk(expr) if isinstance(node, ast.Name)}):
    found = []
    if call is not None and name in function.parameters:
      position = function.parameters.index(name) - (1 if function.owner else 0)
      given = next((keyword.value for keyword in call.keywords if keyword.arg == name), None)
      if given is None and 0 <= position < len(call.args):
        given = call.args[position]
      if given is not None:
        found.append((given, caller))
    for node in own_nodes(function):
      if isinstance(node, ast.Assign) and any(
              (isinstance(target, ast.Name) and target.id == name)
              or (isinstance(target, ast.Tuple) and any(isinstance(item, ast.Name) and item.id == name for item in target.elts))
              for target in node.targets):
        found.append((node.value, function))
      elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in ("add", "update")
            and isinstance(node.func.value, ast.Name) and node.func.value.id == name and node.args):
        found.append((node.args[0], function))
    for value, owner in found:
      held = segment(owner.module.source, value, 90)
      if held == name:
        continue
      notes.append(f"{name}={held}")
      if depth > 1 and owner is function:
        inner = explain(function, value, call, caller, depth - 1)
        if inner:
          notes.append(inner)
  return "; ".join(dict.fromkeys(notes))


def task_bodies(resolve, function, value):
  if isinstance(value, ast.Attribute) and isinstance(value.value, ast.Name) and value.value.id == "self":
    return resolve(function, "self", value.attr, function.owner)
  if isinstance(value, ast.Attribute) and isinstance(value.value, ast.Call) and isinstance(value.value.func, ast.Name):
    return resolve(function, "class", value.attr, value.value.func.id)
  if isinstance(value, ast.Name):
    return resolve(function, "function", value.id, None)
  return []


def main():
  root = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "app")
  wanted = set(sys.argv[2:]) or set(SECTIONS)
  functions = collect(root)
  resolve = resolver(functions)
  callers = defaultdict(set)
  for function in functions:
    for kind, name, owner, _, _ in function.calls:
      for callee in resolve(function, kind, name, owner):
        if callee is not function:
          callers[callee].add(function)

  def text(function, keywords, key, default="", width=160):
    return segment(function.module.source, keywords[key], width) if key in keywords else default

  # Each task body with the enqueues that queue it; an enqueue whose task is a parameter is a wrapper, and the
  # argument its callers pass for it is the body.
  tasks, rows, wrappers = defaultdict(list), [], {}
  for function in functions:
    for line, keywords in function.enqueues:
      site = f"{function.module.path}:{line}"
      scopes, policy = text(function, keywords, "scopes", "—"), text(function, keywords, "on_conflict", "DEDUP")
      task = keywords.get("task")
      if not (isinstance(task, ast.Name) and task.id in function.parameters):
        held = " ".join(explain(function, keywords[key]) for key in ("scopes", "on_conflict") if key in keywords)
        scopes = f"{scopes} [{held}]" if held else scopes
      rows.append(f"{site}  {function.name}  key={text(function, keywords, 'key', '—')}"
                  f"{'  name=' + text(function, keywords, 'name') if 'name' in keywords else ''}  scopes={scopes}"
                  f"  {policy.replace('OnConflict.', '')}  lane={text(function, keywords, 'executor')}"
                  f"  quiet={text(function, keywords, 'quiet', 'False')}  task={text(function, keywords, 'task')}")
      if isinstance(task, ast.Name) and task.id in function.parameters:
        wrappers[function] = (task.id, keywords, site)
      for body in task_bodies(resolve, function, task):
        tasks[body].append((site, scopes, policy))
  for function in functions:
    for kind, name, owner, node, argument in function.calls:
      if argument is not None:
        continue
      for wrapper in resolve(function, kind, name, owner):
        if wrapper not in wrappers:
          continue
        parameter, keywords, site = wrappers[wrapper]
        position = wrapper.parameters.index(parameter) - (1 if wrapper.owner else 0)
        given = next((keyword.value for keyword in node.keywords if keyword.arg == parameter), None)
        given = given if given is not None else node.args[position] if len(node.args) > position else None
        scopes = text(wrapper, keywords, "scopes", "—")
        held = explain(wrapper, keywords["scopes"], node, function) if "scopes" in keywords else ""
        scopes = f"{scopes} [{held}]" if held else scopes
        policy = text(wrapper, keywords, "on_conflict", "DEDUP")
        rule = explain(wrapper, keywords["on_conflict"], node, function) if "on_conflict" in keywords else ""
        policy = f"{policy} [{rule}]" if rule else policy
        for body in task_bodies(resolve, function, given):
          tasks[body].append((f"{function.module.path}:{node.lineno} via {wrapper.name}", scopes, policy))
          rows.append(f"{function.module.path}:{node.lineno}  {function.name}  via {wrapper.name}  task={body.name}"
                      f"  scopes={scopes}  {policy}")

  def reaching(function, depth=12):
    """The task bodies that reach the function, and the roots without a task above them that call it."""
    seen, frontier, found, roots = {function}, [function], {}, set()
    for _ in range(depth):
      following = []
      for current in frontier:
        if current in tasks:
          found[current] = tasks[current]
          continue
        above = callers.get(current, ())
        if not above:
          roots.add(f"route {current.name}" if current.module.name.startswith("routes/")
                    else f"{current.name}, which is passed as a value" if current.parent is not None else current.name)
        for caller in above:
          if caller not in seen:
            seen.add(caller)
            following.append(caller)
      frontier = following
    return found, roots

  if "enqueues" in wanted:
    print(f"## enqueues ({len(rows)}): site, request function, key, name, scopes, policy, lane, quiet, task")
    print("\n".join(sorted(rows)), end="\n\n")

  if "effects" in wanted:
    remote = [function for function in functions if function.remote]
    print(f"## effects ({sum(len(function.remote) for function in remote)} remote calls in {len(remote)} functions):"
          " each with the tasks that reach it (inline calls followed up to a task body)")
    for function in sorted(remote, key=lambda f: f.where):
      found, roots = reaching(function)
      print(f"{function.where}  {function.name}")
      for provider, method, line, summary in sorted(function.remote, key=lambda item: item[2]):
        print(f"    {line}  {provider}.{method}({summary})")
      for body, sites in sorted(found.items(), key=lambda item: item[0].name):
        for site, scopes, policy in sites:
          print(f"    ← task {body.name}  scopes={scopes}  {policy.replace('OnConflict.', '')}  ({site})")
      for name in sorted(roots):
        print(f"    ← no task: reached from {name}")
    print()

  if "locks" in wanted:
    locks = sorted(((function, lock) for function in functions for lock in function.locks),
                   key=lambda item: (item[0].module.path, item[1][0]))
    print(f"## locks ({len(locks)}): each block that holds a lock and what it calls (* requests work, checks or publishes)")
    for function, (line, lock, called) in locks:
      print(f"{function.module.path}:{line}  {function.name}  with {lock}: {marked(called)}")
    print()

  if "readers" in wanted:
    readers = sorted((function for function in functions if function.queue_reads and function.module.name != "utils/queue"),
                     key=lambda f: f.where)
    print(f"## readers ({len(readers)}): what reads the queue, and the params it reads")
    for function in readers:
      params = f"  params: {', '.join(sorted(function.params_read))}" if function.params_read else ""
      print(f"{function.where}  {function.name}  {', '.join(sorted(function.queue_reads))}{params}")
    print()

  if "facts" in wanted:
    print("## facts: each Fact and what its callback calls (* requests work, checks or publishes)")
    for function in functions:
      for line, node in function.facts:
        source, keywords = function.module.source, {keyword.arg: keyword.value for keyword in node.keywords}
        compute = segment(source, node.args[0], 70) if node.args else "—"
        print(f"{function.module.path}:{line}  {function.name}  compute={compute}"
              f"{'  source=' + segment(source, keywords['source'], 40) if 'source' in keywords else ''}")
        callback = keywords.get("on_change")
        for target in task_bodies(resolve, function, callback) if callback is not None else ():
          print(f"    on_change {target.name}: {marked(sorted({name for _, name, _, _, _ in target.calls}))}")
    checks = sorted(((function, check) for function in functions for check in function.fact_checks),
                    key=lambda item: (item[0].module.path, item[1][0]))
    print(f"### checks ({len(checks)}): where a Fact is checked")
    for function, (line, receiver) in checks:
      print(f"{function.module.path}:{line}  {function.name}  {receiver}.check()")
    print()

  if "decisions" in wanted:
    decisions = sorted((function for function in functions if function.module.path.endswith("/decisions.py")
                        and function.owner is None and function.parent is None and not function.node.name.startswith("_")),
                       key=lambda f: f.where)
    print(f"## decisions ({len(decisions)}): each decision and the functions that use it")
    for function in decisions:
      users = sorted(caller.name for caller in callers.get(function, ()))
      print(f"{function.where}  {function.name}  ← {', '.join(users) or 'no caller found'}")
    comparisons = sorted(((function, item) for function in functions for item in function.comparisons),
                         key=lambda item: (item[0].module.path, item[1][0]))
    print(f"### an owner's raw fact compared in a workflow or route ({len(comparisons)})")
    for function, (line, text_) in comparisons:
      print(f"{function.module.path}:{line}  {function.name}  {text_}")


if __name__ == "__main__":
  main()
