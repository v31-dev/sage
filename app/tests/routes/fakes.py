import inspect

from services.manager import Manager


class ManagerStandIn:
  """The Manager as the routes reach it. A call is checked against the real method's signature and recorded as
  (name, args, kwargs); it answers what `answers` holds under its name (an exception is raised, a function is called
  with the arguments), else a task id. A method the real Manager lacks raises, so the stand-in cannot drift from it."""

  def __init__(self):
    self.state = "running"
    self.calls = []
    self.answers = {}

  def task_state(self):
    return self.state

  def __getattr__(self, name):
    real = getattr(Manager, name, None)
    if not callable(real):
      raise AttributeError(f"Manager has no method {name}")

    def call(*args, **kwargs):
      inspect.signature(real).bind(None, *args, **kwargs)
      self.calls.append((name, args, kwargs))
      answer = self.answers.get(name, f"task-{len(self.calls)}")
      if isinstance(answer, Exception):
        raise answer
      return answer(*args, **kwargs) if callable(answer) else answer
    call.__name__ = name
    return call

  def called(self, name):
    """The arguments of each call of `name`, in order."""
    return [(args, kwargs) for called, args, kwargs in self.calls if called == name]
