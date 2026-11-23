"""Normalisation de texte (citations, recherche plein texte)."""
from __future__ import annotations

import re
import unicodedata

_QUOTES = {
    "’": "'", "‘": "'", "ʼ": "'", "`": "'",
    "“": '"', "”": '"', "«": '"', "»": '"',
    "–": "-", "—": "-", "‑": "-", " ": " ", " ": " ",
    "…": "...", "œ": "oe", "Œ": "OE",
}


def normalize(text: str) -> str:
    """Forme comparable : NFKC, guillemets/tirets unifiés, minuscules, espaces compactés."""
    text = unicodedata.normalize("NFKC", text or "")
    for a, b in _QUOTES.items():
        text = text.replace(a, b)
    text = text.lower()
    return re.sub(r"\s+", " ", text).strip()


def strip_accents(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", text) if unicodedata.category(c) != "Mn")


def loose(text: str) -> str:
    """Forme tolérante : sans accents ni ponctuation (pour une seconde tentative de vérification)."""
    t = strip_accents(normalize(text))
    t = re.sub(r"[^\w%]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


_WORD = re.compile(r"[\w%]+", re.UNICODE)


def fts_query(terms: list[str]) -> str:
    """Construit une requête FTS5 : OU entre expressions, chaque expression entre guillemets."""
    parts = []
    for term in terms:
        words = _WORD.findall(strip_accents(term.lower()))
        if not words:
            continue
        parts.append('"' + " ".join(words) + '"')
    return " OR ".join(dict.fromkeys(parts))


def sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?;:])\s+|\n+", text)
    return [p.strip() for p in parts if len(p.strip()) > 3]
