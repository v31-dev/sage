import threading


class Singleton(type):
  """
  A thread-safe implementation of Singleton with per-class locks.
  """

  _instances = {}
  _locks = {}
  _locks_lock = threading.Lock()  # Global lock to protect access to _locks dict

  def __call__(cls, *args, **kwargs):
    # Thread-safe way to get or create a lock for this class
    if cls not in cls._locks:
      with cls._locks_lock:
        if cls not in cls._locks:
          cls._locks[cls] = threading.Lock()

    lock = cls._locks[cls]

    # Use the per-class lock for singleton creation
    if cls not in cls._instances:
      with lock:
        if cls not in cls._instances:
          try:
            instance = super().__call__(*args, **kwargs)
            cls._instances[cls] = instance
          except Exception as e:
            raise Exception(f"Failed to connect to {cls.__name__}: {e}")
    return cls._instances[cls]


class Base(metaclass=Singleton):
  """
  Base class for all services. Initializes a reentrant lock for thread-safe mutation.
  Enforces that subclasses must call super().__init__().
  """

  def __init__(self):
    # RLock allows the same thread to re-acquire without deadlocking.
    self.lock = threading.RLock()
    object.__setattr__(self, "_initialized", True)

  def __init_subclass__(cls, **kwargs):
    super().__init_subclass__(**kwargs)
    original_init = cls.__init__

    def checked_init(self, *args, **kwargs):
      object.__setattr__(self, "_initialized", False)
      original_init(self, *args, **kwargs)
      if not object.__getattribute__(self, "_initialized"):
        raise RuntimeError(f"{cls.__name__}.__init__() must call super().__init__() first.")

    cls.__init__ = checked_init
