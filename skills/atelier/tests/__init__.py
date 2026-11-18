"""Atelier tests.

The OMM cache lookup runs node and omm when they are installed. Point it at a script that does not
exist so every run starts without OMM; tests that exercise OMM patch atelier.survey themselves.
"""

import os

os.environ.setdefault("ATELIER_OMM_SCRIPT", os.path.join(os.path.dirname(__file__), "no-omm-cache.mjs"))
