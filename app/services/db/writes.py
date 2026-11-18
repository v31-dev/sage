from contextlib import contextmanager

from services.db.models import db


@contextmanager
def write():
  """A resource command's transaction, which takes SQLite's write lock before anything is read."""
  if db.in_transaction():
    raise RuntimeError("A command runs in its own transaction, so that it publishes only after its commit.")
  with db.atomic("IMMEDIATE"):
    yield


def trim(value):
  return value.strip() if isinstance(value, str) else value


def get(model, *where):
  row = model.get_or_none(*where)
  if row is None:
    raise LookupError(f"{model.__name__} not found.")
  return row


def merged(row, fields: dict, allowed: tuple[str, ...]) -> dict:
  """The `allowed` values of `row` with the sent `fields` over them, trimmed."""
  if unknown := set(fields) - set(allowed):
    raise ValueError(f"Unknown fields: {', '.join(sorted(unknown))}.")
  config = {name: getattr(row, name) for name in allowed}
  for name, value in fields.items():
    if value is None and not row._meta.fields[name].null:
      raise ValueError(f"{name} is required.")
    config[name] = trim(value)
  return config


def save_changes(row, config: dict) -> bool:
  """Writes the values of `config` that differ from `row`, and says whether there were any."""
  changed = {name: value for name, value in config.items() if getattr(row, name) != value}
  for name, value in changed.items():
    setattr(row, name, value)
  if changed:
    row.save()
  return bool(changed)
