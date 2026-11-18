import ast
import inspect
import sys
from pathlib import Path

from services.manager import Manager

PACKAGE = Path(inspect.getsourcefile(Manager)).parent


def test_the_manager_is_made_of_every_mixin_of_its_package():
  mixins = {node.name for path in PACKAGE.glob("*.py") for node in ast.parse(path.read_text()).body
            if isinstance(node, ast.ClassDef) and node.name.endswith("Mixin")}
  assert mixins - {base.__name__ for base in Manager.__mro__} == set()


def test_the_manager_gives_the_queue_every_owners_facts():
  owners = [name for name in dir(Manager) if name.endswith("_queue_facts")]
  source = inspect.getsource(sys.modules[Manager.__module__])
  assert len(owners) >= 6
  assert [name for name in owners if f"self.{name}()" not in source] == []
