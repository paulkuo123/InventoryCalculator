"""Allow ``python -m mapping_knowledge seed-aliases`` beside the data directory."""

from __future__ import annotations

import runpy
from pathlib import Path

_MODULE = Path(__file__).resolve().parent.parent / "mapping_knowledge.py"
runpy.run_path(str(_MODULE), run_name="__main__")
