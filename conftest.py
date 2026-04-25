"""Root conftest for `api/` test suite.

Adds the api/ directory to sys.path so tests can import modules as top-level
(e.g. `from config import settings`). Also re-exports the shared fixture from
`tests/conftest.py` by relying on pytest's auto-discovery (nothing to do here).
"""

import os
import sys
from pathlib import Path

# Skip the B-005 startup secret validation in tests by default. Tests that
# intentionally exercise the validation (e.g. test_config.py) clear this
# env var before reloading config.
os.environ.setdefault("RATIOVAULT_SKIP_SECRET_VALIDATION", "1")

sys.path.insert(0, str(Path(__file__).parent))
