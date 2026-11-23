"""Chargement de la configuration et du référentiel."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Config:
    raw: dict[str, Any]

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self.raw
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    @property
    def backend(self) -> str:
        # La variable d'environnement permet de forcer le mode test.
        return os.environ.get("PREAUDIT_BACKEND", self.get("backend", "ollama"))

    @property
    def missions_dir(self) -> Path:
        p = Path(self.get("missions_dir", "missions"))
        return p if p.is_absolute() else ROOT / p

    @property
    def referentiel_path(self) -> Path:
        p = Path(self.get("referentiel"))
        return p if p.is_absolute() else ROOT / p


def load_config(path: str | Path | None = None) -> Config:
    path = Path(path) if path else ROOT / "config.yaml"
    with open(path, encoding="utf-8") as f:
        return Config(yaml.safe_load(f))


def load_referentiel(cfg: Config) -> dict[str, Any]:
    with open(cfg.referentiel_path, encoding="utf-8") as f:
        ref = yaml.safe_load(f)
    ids = [e["id"] for e in ref["exigences"]]
    if len(ids) != len(set(ids)):
        raise ValueError("Identifiants d'exigences en double dans le référentiel")
    return ref


def annex_a_controls() -> list[str]:
    """Identifiants des 93 mesures de l'Annexe A (version 2022)."""
    counts = {5: 37, 6: 8, 7: 14, 8: 34}
    return [f"A.{t}.{i}" for t, n in counts.items() for i in range(1, n + 1)]
