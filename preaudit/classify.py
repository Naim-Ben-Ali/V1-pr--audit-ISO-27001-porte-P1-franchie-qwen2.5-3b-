"""Typage des documents et extraction des métadonnées d'information documentée (7.5)."""
from __future__ import annotations

import re
from datetime import date

from .parsers import ParsedDoc, Table
from .text_utils import strip_accents

CONTROL_ID = re.compile(r"^\s*A\s*\.?\s*(\d{1,2})\s*\.\s*(\d{1,2})\b", re.I)


_STOP = {"de", "du", "des", "la", "le", "les", "l", "d", "et", "a", "au", "aux", "en", "pour", "the", "of", "and"}


def _norm(s: str) -> str:
    return strip_accents((s or "").lower())


def _kw(s: str) -> str:
    """Forme de comparaison sans accents, ponctuation ni mots vides (« plan de traitement » = « Plan_Traitement »)."""
    words = re.findall(r"[a-z0-9]+", _norm(s))
    return " " + " ".join(w for w in words if w not in _STOP) + " "


def find_soa_table(tables: list[Table]) -> Table | None:
    """Un tableau est une SoA s'il liste ≥ 10 identifiants de mesures « A.x.y »."""
    best, best_n = None, 0
    for t in tables:
        for col in range(len(t.header)):
            n = sum(1 for r in t.rows if col < len(r) and CONTROL_ID.match(r[col] or ""))
            if n > best_n:
                best, best_n = t, n
    return best if best_n >= 10 else None


def find_risk_table(tables: list[Table]) -> Table | None:
    for t in tables:
        h = " ".join(_norm(x) for x in t.header)
        if "risque" in h or "risk" in h:
            if any(k in h for k in ("proprietaire", "owner", "vraisemblance", "probabilite", "impact", "niveau")):
                return t
    return None


def classify(filename: str, parsed: ParsedDoc, types: dict) -> tuple[str, float]:
    """Renvoie (type, score). Le nom du fichier pèse plus que le contenu."""
    if find_soa_table(parsed.tables):
        return "soa", 10.0
    name = _kw(filename.rsplit(".", 1)[0])
    head = _kw(" ".join(b.section + " " + b.text for b in parsed.blocks[:6])[:2500])
    scores: dict[str, float] = {}
    for tid, spec in types.items():
        s = 0.0
        for kw in spec.get("mots_cles", []):
            k = _kw(kw)
            if not k.strip():
                continue
            if k in name:
                s += 3.0
            s += min(head.count(k), 3) * 1.0
        if s:
            scores[tid] = s
    if find_risk_table(parsed.tables):
        scores["registre_risques"] = scores.get("registre_risques", 0) + 4
    if not scores:
        return "autre", 0.0
    best = max(scores, key=scores.get)
    # « procédure » et « enregistrement » sont génériques : ils ne gagnent qu'à défaut d'autre chose.
    generic = {"procedure", "enregistrement"}
    specific = {k: v for k, v in scores.items() if k not in generic}
    if best in generic and specific and max(specific.values()) >= scores[best] * 0.6:
        best = max(specific, key=specific.get)
    return best, scores[best]


# ------------------------------------------------------------ métadonnées
_DATE = r"(\d{4}-\d{2}-\d{2}|\d{1,2}[/.\-]\d{1,2}[/.\-]\d{4})"
_PATTERNS = {
    "version": re.compile(r"\b(?:version|r[ée]vision|rev\.?|indice)\s*(?:n°|no|:|-)?\s*v?(\d+(?:[.,]\d+){0,2})\b", re.I),
    "approved_by": re.compile(
        r"(?:approuv[ée]e?s?|valid[ée]e?s?|approved|sign[ée]e?)\s*(?:par|by)\s*[:\-]?\s*([^\n|;]{2,60})", re.I),
    "doc_date": re.compile(
        r"(?:\bdate\b[^:\n\d]{0,40}|cr[ée]{1,2}\s*le|mis\s*[àa]\s*jour\s*le|en\s*date\s*du)\s*[:\-]?\s*" + _DATE, re.I),
    "review_date": re.compile(
        r"(?:prochaine\s*(?:revue|r[ée]vision)|date\s*de\s*(?:revue|r[ée]vision)\s*pr[ée]vue|"
        r"r[ée]vision\s*pr[ée]vue|next\s*review)\s*(?:le|:|-)?\s*" + _DATE, re.I),
}
_DRAFT = re.compile(r"\b(brouillon|draft|projet de document|version provisoire|en cours de r[ée]daction)\b", re.I)


def parse_date(s: str) -> date | None:
    s = s.strip()
    try:
        if re.match(r"\d{4}-\d{2}-\d{2}", s):
            y, m, d = map(int, s[:10].split("-"))
        else:
            d, m, y = map(int, re.split(r"[/.\-]", s))
        return date(y, m, d)
    except (ValueError, TypeError):
        return None


def extract_metadata(parsed: ParsedDoc) -> dict:
    head = "\n".join(b.text for b in parsed.blocks[:8])[:4000]
    # Les tableaux de gestion documentaire en tête de document sont aussi lus.
    for t in parsed.tables[:2]:
        if t.header == ["Champ", "Valeur"]:
            head += "\n" + "\n".join(f"{r[0]} : {r[1]}" for r in t.rows)
        else:
            head += "\n" + "\n".join(f"{h}: {v}" for r in t.rows[:6] for h, v in zip(t.header, r))
    meta: dict = {"version": "", "approved_by": "", "doc_date": "", "review_date": "", "status": "inconnu"}
    for key, pat in _PATTERNS.items():
        m = None
        for cand in pat.finditer(head):
            # une « date de revue prévue » n'est pas la date du document
            if key == "doc_date" and re.search(r"revue|r[ée]vision|review", cand.group(0), re.I) \
                    and re.search(r"pr[ée]vue|prochaine|next", cand.group(0), re.I):
                continue
            m = cand
            break
        if m:
            val = m.group(1).strip(" .:-")
            if key == "approved_by":
                val = re.split(r"\.\s|\s[-–]\s|\s{2,}|,\s*(?=date|version|prochaine)", val, flags=re.I)[0].strip(" .:-")
            if key in ("doc_date", "review_date"):
                d = parse_date(val)
                val = d.isoformat() if d else ""
            meta[key] = val
    # Repli sur les propriétés natives du fichier (moins fiables : signalées comme telles)
    if not meta["version"] and parsed.props.get("revision") not in ("", "1", None):
        meta["version"] = parsed.props["revision"] + " (propriété du fichier)"
    if _DRAFT.search(head):
        meta["status"] = "brouillon"
    elif meta["approved_by"]:
        meta["status"] = "approuvé"
    return meta
