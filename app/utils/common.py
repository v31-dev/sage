import logging
import os
import re
from types import SimpleNamespace

logger = logging.getLogger(__name__)


DOMAIN_RE = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$",
    re.IGNORECASE,
)
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def dict_to_obj(data):
  """
  Convert a dictionary to an object with named attributes
  """
  return SimpleNamespace(**data)


def get_env(name: str) -> str:
  """
  Helper function to get a required environment variable.
  """
  value = os.getenv(name)

  if value is None or value == "":
    raise ValueError(f"Environment variable '{name}' is required but not set.")

  return value


def update_config_file(path, config):
  """
  Update a config file only if the contents have changed.
  File will be created if it doesn't exist.
  """
  if not os.path.exists(path):
    with open(path, "w") as f:
      f.write(config)
    logger.info(f"Config file '{path}' created.")
    return True
  else:
    with open(path, "r") as f:
      current_config = f.read()
      if current_config != config:
        with open(path, "w") as f:
          f.write(config)
        logger.info(f"Config file '{path}' updated.")
        return True
      else:
        logger.info(f"Config file '{path}' is unchanged.")
        return False
