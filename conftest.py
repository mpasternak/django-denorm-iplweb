"""Root conftest for the deadlock / race-condition test suite.

Spins up a real PostgreSQL via pytest-testcontainers-django so we can hit
the actual deadlock paths in `denorm.denorms.flush_single`.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).parent
TEST_PROJECT = ROOT / "test_denorm_project"

for p in (str(ROOT), str(TEST_PROJECT)):
    if p not in sys.path:
        sys.path.insert(0, p)
