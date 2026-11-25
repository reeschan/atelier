"""Atelier tests.

The OMM cache lookup runs node and omm when they are installed. Point it at a script that does not
exist so every run starts without OMM; tests that exercise OMM patch atelier.survey themselves.

A run that locks its acceptance tests writes the lock hash under ATELIER_LOCKS_HOME (by default
~/.config/atelier/locks). Point it at a temporary folder, removed at exit, so no test writes there.
"""

import atexit
import os
import shutil
import tempfile

os.environ.setdefault("ATELIER_OMM_SCRIPT", os.path.join(os.path.dirname(__file__), "no-omm-cache.mjs"))

_LOCKS_HOME = tempfile.mkdtemp(prefix="atelier-locks-")
atexit.register(shutil.rmtree, _LOCKS_HOME, True)
os.environ["ATELIER_LOCKS_HOME"] = _LOCKS_HOME
