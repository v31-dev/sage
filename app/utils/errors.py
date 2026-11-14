class Conflict(Exception):
  """The current state refuses the request: a duplicate, or deleting something still in use."""
