from pathlib import Path

# This module lives at app/services/manager/_common.py, so three .parent hops
# reach the app/ root. Defined here once so every mixin shares the same path.
app_dir = Path(__file__).parent.parent.parent
