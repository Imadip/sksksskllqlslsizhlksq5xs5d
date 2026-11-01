"""Small JSON-backed English/Arabic translation loader."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def merge_translations(base: dict[str, dict[str, str]]) -> dict[str, dict[str, str]]:
    merged = {key: dict(value) for key, value in base.items()}
    locale_dir = Path(__file__).with_name("locales")
    for language in ("en", "ar"):
        path = locale_dir / f"{language}.json"
        with path.open(encoding="utf-8") as locale_file:
            translations: dict[str, Any] = json.load(locale_file)
        for key, text in translations.items():
            merged.setdefault(key, {})[language] = text
    return merged
