"""Root conftest for `api/` test suite.

Adds the api/ directory to sys.path so tests can import modules as top-level
(e.g. `from config import settings`). Also re-exports the shared fixture from
`tests/conftest.py` by relying on pytest's auto-discovery (nothing to do here).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
