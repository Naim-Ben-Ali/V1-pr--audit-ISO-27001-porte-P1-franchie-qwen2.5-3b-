"""Rapport généré par gabarit (pas de texte libre du modèle) : Markdown, puis DOCX via Pandoc local."""
from __future__ import annotations

import shutil
import subprocess
from collections import Counter
from datetime import datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from .classes import DOC, NC_MAJ, NC_MIN, OBS, OFI, OK, RANK, REVIEW, STATUS_LABELS
from .config import Config, load_referentiel
from .store import Store

TEMPLATES = Path(__file__).parent / "templates"


def _md(s) -> str:
    """Neutralise ce qui casserait une cellule de tableau Markdown."""
    return str(s or "").replace("|", "\\|").replace("\n", " ").strip()


def _effective(kind: str, item_id: str, classification: str, decisions: dict) -> tuple[str, dict | None]:
    d = decisions.get((kind, item_id))
    if d and d["decision"] == "modifié" and d["classification_override"]:
        return d["classification_override"], d
    return classification, d


def build_context(store: Store, cfg: Config, run_id: int, org: str, include_unreviewed: bool = True) -> dict:
    ref = load_referentiel(cfg)
    reqs = {r["id"]: r for r in ref["exigences"]}
    types = ref["types_documents"]
    run = dict(store.run(run_id))
    decisions = store.decisions(run_id)
    findings = store.findings(run_id)
    checks = store.checks(run_id)

    items, matrix, stage2 = [], [], []
    n_unreviewed = n_reviewed = 0
    theme_counts: dict[str, Counter] = {}

    for f in findings:
        req = reqs.get(f["req_id"], {})
        cls, dec = _effective("finding", f["req_id"], f["classification"], decisions)
        rejected = bool(dec and dec["decision"] == "rejeté")
        review = dec["decision"] if dec else "non revu"
        if dec:
            n_reviewed += 1
        elif cls != DOC:
            n_unreviewed += 1
        matrix.append(dict(req_id=f["req_id"], source=f["source"], status_label=STATUS_LABELS.get(f["status"], f["status"]),
                           classification=cls if not rejected else f"{cls} (rejeté)", review=review,
                           docs=", ".join(dict.fromkeys(c["document"] for c in f["citations"])) or "—"))
        if rejected:
            continue
        theme = req.get("theme", "Autre")
        tc = theme_counts.setdefault(theme, Counter())
        tc["total"] += 1
        tc[{NC_MAJ: "nc_maj", NC_MIN: "nc_min", OBS: "obs", OFI: "obs", REVIEW: "review", DOC: "doc"}.get(cls, "obs")] += 1
        if f["questions"]:
            stage2.append(dict(req_id=f["req_id"], label=STATUS_LABELS.get(f["status"], ""), questions=f["questions"]))
        if cls == DOC or (not dec and not include_unreviewed):
            continue
        items.append(dict(ref=f["req_id"], title=req.get("exigence", ""), source=f["source"], classification=cls,
                          overridden=bool(dec and dec["decision"] == "modifié"),
                          status_label=STATUS_LABELS.get(f["status"], f["status"]), confidence=f["confidence"],
                          rationale=f["rationale"], missing=f["missing"], citations=f["citations"],
                          detail=f["rationale"] or STATUS_LABELS.get(f["status"], ""),
                          comment=dec["comment"] if dec else "", unreviewed=not dec))

    checks_view = []
    for c in checks:
        cls, dec = _effective("check", c["check_id"], c["classification"], decisions)
        label = cls + (" (rejeté par l'auditeur)" if dec and dec["decision"] == "rejeté" else
                       " (modifié par l'auditeur)" if dec and dec["decision"] == "modifié" else "")
        checks_view.append({**c, "classification": label})
        if c["status"] == "ok" and not dec:
            continue
        if dec:
            n_reviewed += 1
        else:
            n_unreviewed += 1
        if (dec and dec["decision"] == "rejeté") or cls == OK or (not dec and not include_unreviewed):
            continue
        items.append(dict(ref=c["check_id"], title=c["title"], source=c["source"], classification=cls,
                          overridden=bool(dec and dec["decision"] == "modifié"), status_label="", confidence="",
                          rationale=c["detail"], missing=[], citations=[], detail=c["detail"],
                          comment=dec["comment"] if dec else "", unreviewed=not dec))

    items.sort(key=lambda x: (RANK.get(x["classification"], 99), x["source"], x["ref"]))
    ncs = [i for i in items if i["classification"] in (NC_MAJ, NC_MIN)]
    observations = [i for i in items if i["classification"] in (OBS, OFI, REVIEW)]

    themes = []
    for name in dict.fromkeys(r["theme"] for r in ref["exigences"]):
        tc = theme_counts.get(name, Counter())
        themes.append(dict(name=name, **{k: tc[k] for k in ("total", "nc_maj", "nc_min", "obs", "review", "doc")}))
    totals = {k: sum(t[k] for t in themes) for k in ("total", "nc_maj", "nc_min", "obs", "review", "doc")}

    cc = Counter(c["classification"] for c in checks)
    checks_summary = ", ".join(f"{n} × {k}" for k, n in sorted(cc.items(), key=lambda x: RANK.get(x[0], 99))) or "aucun"

    n_maj = sum(1 for i in ncs if i["classification"] == NC_MAJ)
    n_min = len(ncs) - n_maj
    worst = sorted({i["source"] for i in ncs if i["classification"] == NC_MAJ})
    summary = (f"La revue documentaire relève {n_maj} non-conformité(s) majeure(s) potentielle(s) et {n_min} mineure(s) "
               f"potentielle(s), ainsi que {len(observations)} observation(s), exigences et contrôles automatiques confondus.")
    if worst:
        summary += " Les écarts majeurs portent sur les clauses " + ", ".join(worst) + "."

    docs = []
    for d in store.documents():
        dd = dict(d)
        dd["type_label"] = types.get(d["eff_type"], {}).get("libelle", d["eff_type"])
        docs.append(dd)

    return dict(org=org, generated_at=datetime.now().strftime("%Y-%m-%d %H:%M"), run=run,
                ref_version=f"{ref['norme']} — référentiel v{ref['version']}", n_reqs=len(ref["exigences"]),
                draft=n_unreviewed > 0, n_unreviewed=n_unreviewed, n_reviewed=n_reviewed,
                themes=themes, totals=totals, ncs=ncs, observations=observations,
                checks=checks_view, checks_summary=checks_summary, stage2=stage2,
                matrix=matrix, docs=docs, executive_summary=summary)


def render_markdown(ctx: dict) -> str:
    env = Environment(loader=FileSystemLoader(TEMPLATES), trim_blocks=False, lstrip_blocks=False,
                      keep_trailing_newline=True)
    env.filters["md"] = _md
    return env.get_template("rapport.md.j2").render(**ctx)


def export(store: Store, cfg: Config, run_id: int, org: str, out_dir: str | Path,
           include_unreviewed: bool = True) -> dict[str, Path]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    ctx = build_context(store, cfg, run_id, org, include_unreviewed)
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    base = out_dir / f"rapport_preaudit_analyse{run_id}_{stamp}{'_BROUILLON' if ctx['draft'] else ''}"
    md_path = base.with_suffix(".md")
    md_path.write_text(render_markdown(ctx), encoding="utf-8")
    paths = {"md": md_path}
    pandoc = shutil.which("pandoc")
    if pandoc:                       # conversion 100 % locale
        docx_path = base.with_suffix(".docx")
        subprocess.run([pandoc, str(md_path), "-o", str(docx_path), "--from", "markdown"], check=True,
                       capture_output=True)
        paths["docx"] = docx_path
    return paths
