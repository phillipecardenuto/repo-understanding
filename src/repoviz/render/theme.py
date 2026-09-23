"""Visual conventions shared by the Python renderer and the browser UI (``web/theme.json``)."""

from __future__ import annotations

import json
from functools import lru_cache
from importlib import resources
from typing import Any


@lru_cache(maxsize=1)
def theme() -> dict[str, Any]:
    return json.loads(resources.files("repoviz").joinpath("web/theme.json").read_text(encoding="utf-8"))
