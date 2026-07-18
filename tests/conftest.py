"""Force the entire test suite onto an isolated throwaway database.

INCIDENT GUARD. Several test modules exercise destructive paths (``DELETE FROM
jobs``, lane deletion, dedupe). Individually they try to isolate themselves, but
they used two *incompatible* strategies: some set ``os.environ['JSE_DATA_DIR']``
at import time, others reassign ``database_manager.DB_FILE`` and restore it to
whatever it was when the class started. When the whole suite runs in one pytest
process, ``database_manager`` gets imported (resolving ``DB_FILE`` to the real
``settings/job_applications.db``) by whichever module is collected first, so a
later module's env override arrives too late — and its ``DELETE FROM jobs`` then
hits the real database. That wiped a production database once; never again.

pytest imports this conftest before collecting any test module, so setting
``JSE_DATA_DIR`` here guarantees ``database_manager.DB_FILE`` resolves to a
throwaway directory for every module regardless of import order. We then import
the modules and hard-assert the isolation held.
"""
import os
import tempfile

_ISOLATED_ROOT = tempfile.mkdtemp(prefix="jse_test_dbroot_")
os.environ["JSE_DATA_DIR"] = _ISOLATED_ROOT

import database_manager as _db  # noqa: E402
import db_setup as _db_setup  # noqa: E402

# Belt and suspenders: if anything imported database_manager before this ran,
# force the path to the throwaway root and fail loudly rather than silently
# operating on real data.
_ISOLATED_DB = os.path.join(_ISOLATED_ROOT, "job_applications.db")
_db.DB_FILE = _ISOLATED_DB
_db_setup.DB_FILE = _ISOLATED_DB

assert _ISOLATED_ROOT in _db.DB_FILE, (
    f"Test database is not isolated (DB_FILE={_db.DB_FILE!r}); refusing to run "
    "against a real database."
)
