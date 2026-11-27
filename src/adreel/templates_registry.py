"""Load declarative templates from the built-in directory plus any configured extra dirs."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

import yaml
from pydantic import ValidationError

from adreel.config import BUILTIN_TEMPLATES_DIR
from adreel.schemas.template import TemplateSpec


class TemplateError(Exception):
    pass


def load_template_file(path: Path) -> TemplateSpec:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        return TemplateSpec.model_validate(data)
    except (yaml.YAMLError, ValidationError) as exc:
        raise TemplateError(f"invalid template {path}: {exc}") from exc


class TemplateRegistry:
    def __init__(self, dirs: Iterable[Path] = ()) -> None:
        self._templates: dict[str, TemplateSpec] = {}
        for d in [BUILTIN_TEMPLATES_DIR, *dirs]:
            if not d.is_dir():
                continue
            for p in sorted([*d.glob("*.yaml"), *d.glob("*.yml"), *d.glob("*.json")]):
                spec = load_template_file(p)
                self._templates[spec.id] = spec  # later dirs override built-ins

    def get(self, template_id: str) -> TemplateSpec:
        try:
            return self._templates[template_id]
        except KeyError:
            raise TemplateError(
                f"unknown template {template_id!r}; available: {sorted(self._templates)}"
            ) from None

    def all(self) -> list[TemplateSpec]:
        return [self._templates[k] for k in sorted(self._templates)]
