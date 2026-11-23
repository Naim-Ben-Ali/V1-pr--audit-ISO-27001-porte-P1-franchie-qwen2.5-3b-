"""Contrôles déterministes (sans LLM) : documents obligatoires, information
documentée (7.5), cohérence de la SoA, registre des risques, croisements.

Ces contrôles ne coûtent presque rien et sont souvent ceux qu'un humain rate.
"""
from __future__ import annotations

import re
from datetime import date

from .classes import NC_MAJ, NC_MIN, OBS, OK
from .classify import CONTROL_ID, find_risk_table, find_soa_table, parse_date
from .config import Config, annex_a_controls
from .parsers import Table
from .store import Store
from .text_utils import fts_query, strip_accents

MAJOR_DOCS = {"DO-4.3", "DO-5.2", "DO-6.1.2", "DO-6.1.3", "DO-SOA", "DO-9.2", "DO-9.3"}
CONTROLLED_TYPES = {"politique_ssi", "perimetre", "manuel_smsi", "methodo_risques", "plan_traitement",
                    "soa", "objectifs", "procedure", "maitrise_documentaire", "roles", "communication", "contexte"}
KEY_TYPES = {"politique_ssi", "soa", "methodo_risques", "perimetre"}
GENERIC_JUSTIF = {"", "-", "n/a", "na", "nc", "non applicable", "sans objet", "non", "exclu", "neant", "aucune", "/"}


def _n(s: str) -> str:
    return strip_accents((s or "").lower()).strip()


def _col(header: list[str], *keys: str, exclude: tuple[str, ...] = ()) -> int | None:
    for i, h in enumerate(header):
        nh = _n(h)
        if any(k in nh for k in keys) and not any(x in nh for x in exclude):
            return i
    return None


def _cid(raw: str) -> str | None:
    m = CONTROL_ID.match(raw or "")
    return f"A.{int(m.group(1))}.{int(m.group(2))}" if m else None


def _check(cid, source, title, status, classification, detail, evidence=None) -> dict:
    return dict(check_id=cid, source=source, title=title, status=status,
                classification=classification, detail=detail, evidence=evidence or [])


# ------------------------------------------------------------ documents obligatoires
def check_mandatory_docs(store: Store, ref: dict) -> list[dict]:
    docs = [d for d in store.documents() if not d["error"] and d["eff_type"] != "norme_exclue"]
    out = []
    for do in ref["documents_obligatoires"]:
        typed = [d["filename"] for d in docs if d["eff_type"] in do["types"]]
        if typed:
            out.append(_check(do["id"], do["source"], do["libelle"], "ok", OK,
                              f"Document(s) identifié(s) : {', '.join(typed)}.", typed))
            continue
        hits = store.fts(fts_query(do["mots_cles"]), 20)
        rows = store.chunk_rows([h[0] for h in hits])
        mentioned = sorted({r["filename"] for r in rows.values()})
        if mentioned:
            out.append(_check(do["id"], do["source"], do["libelle"], "a_confirmer", OBS,
                              "Aucun document de ce type identifié ; le sujet est mentionné dans : "
                              + ", ".join(mentioned[:5]) + ". Vérifier si l'information documentée existe.",
                              mentioned[:5]))
        else:
            cls = NC_MAJ if do["id"] in MAJOR_DOCS else NC_MIN
            out.append(_check(do["id"], do["source"], do["libelle"], "ecart", cls,
                              "Aucune information documentée trouvée dans les documents fournis."))
    return out


# ------------------------------------------------------------ information documentée (7.5)
def check_document_metadata(store: Store, cfg: Config, today: date | None = None) -> list[dict]:
    today = today or date.today()
    max_age = int(cfg.get("checks.max_doc_age_months", 12))
    out = []
    for d in store.documents():
        if d["error"] or d["eff_type"] not in CONTROLLED_TYPES:
            continue
        issues = []
        if not d["version"]:
            issues.append("aucune version identifiée")
        if d["status"] == "brouillon":
            issues.append("document marqué comme brouillon")
        elif not d["approved_by"]:
            issues.append("aucune mention d'approbation trouvée")
        rd = parse_date(d["review_date"]) if d["review_date"] else None
        dd = parse_date(d["doc_date"]) if d["doc_date"] else None
        if rd and rd < today:
            issues.append(f"date de revue prévue dépassée ({rd.isoformat()})")
        elif not rd and dd and (today - dd).days > max_age * 30.5:
            issues.append(f"daté du {dd.isoformat()}, plus de {max_age} mois, sans date de revue")
        if not dd:
            issues.append("aucune date identifiée")
        if not issues:
            continue
        serious = d["eff_type"] in KEY_TYPES and (d["status"] == "brouillon" or not d["approved_by"])
        out.append(_check(f"META-{d['filename']}", "7.5", f"Maîtrise documentaire — {d['filename']}",
                          "ecart", NC_MIN if serious else OBS,
                          "Détecté automatiquement, à confirmer à la lecture : " + " ; ".join(issues) + ".",
                          [d["filename"]]))
    return out


# ------------------------------------------------------------ SoA
def _applicable(v: str) -> bool | None:
    n = _n(v)
    if not n:
        return None
    if n in {"non", "no", "n", "exclu", "exclue", "non applicable", "na", "n/a", "non retenu", "non retenue", "false", "0"} \
            or n.startswith(("non ", "exclu")):
        return False
    if n in {"oui", "yes", "o", "y", "x", "applicable", "inclus", "incluse", "retenu", "retenue", "true", "1"} \
            or n.startswith(("oui", "applicable")):
        return True
    return None


def analyse_soa(t: Table) -> dict:
    h = t.header
    id_col = max(range(len(h)), key=lambda c: sum(1 for r in t.rows if c < len(r) and _cid(r[c])))
    app_col = _col(h, "applicab", "inclus", "retenu", "selection", "applicable")
    just_col = _col(h, "justif", "raison", "motif", "reason", exclude=("exclu",))
    just_excl_col = _col(h, "justification de l'exclusion", "justification exclusion", "motif d'exclusion")
    impl_col = _col(h, "mise en oeuvre", "implement", "etat", "statut", exclude=("applicab",))
    rows = []
    for r in t.rows:
        cid = _cid(r[id_col] if id_col < len(r) else "")
        if not cid:
            continue
        get = lambda c: r[c] if c is not None and c < len(r) else ""
        rows.append(dict(id=cid, applicable=_applicable(get(app_col)), justif=get(just_col),
                         justif_excl=get(just_excl_col) or get(just_col), impl=get(impl_col)))
    return dict(rows=rows, cols=dict(id=id_col, app=app_col, just=just_col, impl=impl_col))


def check_soa(store: Store) -> tuple[list[dict], dict | None]:
    soa_tables = []
    for d in store.documents():
        if d["error"]:
            continue
        tabs = [Table(t["name"], t["header"], t["rows"]) for t in store.tables(d["id"])]
        t = find_soa_table(tabs)
        if t:
            soa_tables.append((d["filename"], t))
    if not soa_tables:
        return [], None
    filename, table = max(soa_tables, key=lambda x: len(x[1].rows))
    soa = analyse_soa(table)
    soa["filename"] = filename
    out = []
    ids = [r["id"] for r in soa["rows"]]
    ev = [filename]

    old = sorted({i for i in ids if int(i.split(".")[1]) >= 9})
    if old:
        out.append(_check("SOA-VERSION", "6.1.3 d)", "SoA alignée sur la version 2022", "ecart", NC_MIN,
                          f"Identifiants propres à la version 2013 détectés ({', '.join(old[:6])}…). "
                          "La SoA doit porter sur les 93 mesures de l'édition 2022.", ev))
    missing = [c for c in annex_a_controls() if c not in ids]
    if missing and not old:
        out.append(_check("SOA-COMPLETE", "6.1.3 d)", "Couverture des 93 mesures de l'Annexe A", "ecart", NC_MIN,
                          f"{len(missing)} mesure(s) absente(s) de la SoA : {', '.join(missing[:12])}"
                          + ("…" if len(missing) > 12 else "") + ".", ev))
    elif not old:
        out.append(_check("SOA-COMPLETE", "6.1.3 d)", "Couverture des 93 mesures de l'Annexe A", "ok", OK,
                          "Les 93 mesures figurent dans la SoA.", ev))

    if soa["cols"]["app"] is None:
        out.append(_check("SOA-APPLIC", "6.1.3 d)", "Applicabilité indiquée", "a_confirmer", OBS,
                          "Aucune colonne d'applicabilité reconnue : vérifier la structure de la SoA.", ev))
    else:
        unclear = [r["id"] for r in soa["rows"] if r["applicable"] is None]
        if unclear:
            out.append(_check("SOA-APPLIC", "6.1.3 d)", "Applicabilité indiquée", "ecart", OBS,
                              f"Applicabilité non renseignée ou ambiguë pour : {', '.join(unclear[:12])}.", ev))
        bad = [r["id"] for r in soa["rows"] if r["applicable"] is False and _n(r["justif_excl"]) in GENERIC_JUSTIF
               or (r["applicable"] is False and len(_n(r["justif_excl"])) < 12)]
        excluded = [r["id"] for r in soa["rows"] if r["applicable"] is False]
        if bad:
            out.append(_check("SOA-EXCL", "6.1.3 d)", "Justification des exclusions", "ecart", NC_MIN,
                              f"{len(bad)} exclusion(s) sans justification exploitable : {', '.join(bad)}.", ev))
        elif excluded:
            out.append(_check("SOA-EXCL", "6.1.3 d)", "Justification des exclusions", "ok", OK,
                              f"{len(excluded)} exclusion(s), toutes justifiées : {', '.join(excluded)}.", ev))
        no_incl = [r["id"] for r in soa["rows"] if r["applicable"] and len(_n(r["justif"])) < 4]
        if no_incl:
            out.append(_check("SOA-INCL", "6.1.3 d)", "Justification des inclusions", "ecart", OBS,
                              f"{len(no_incl)} mesure(s) applicable(s) sans justification d'inclusion "
                              f"(ex. {', '.join(no_incl[:8])}).", ev))
        if soa["cols"]["impl"] is not None:
            pending = [r["id"] for r in soa["rows"] if r["applicable"]
                       and re.search(r"non|planifi|pr[eé]vu|a faire|en cours|todo|partiel", _n(r["impl"]))]
            if pending:
                out.append(_check("SOA-IMPL", "6.1.3 d) / 8.3", "État de mise en œuvre des mesures applicables",
                                  "ecart", OBS,
                                  f"{len(pending)} mesure(s) applicable(s) non encore mises en œuvre : "
                                  f"{', '.join(pending[:12])}. Vérifier qu'elles figurent au plan de traitement.", ev))
        else:
            out.append(_check("SOA-IMPL", "6.1.3 d)", "État de mise en œuvre des mesures applicables", "ecart", OBS,
                              "Aucune colonne d'état de mise en œuvre reconnue dans la SoA.", ev))
    return out, soa


# ------------------------------------------------------------ registre des risques
def check_risk_register(store: Store) -> list[dict]:
    out = []
    for d in store.documents():
        if d["error"]:
            continue
        tabs = [Table(t["name"], t["header"], t["rows"]) for t in store.tables(d["id"])]
        t = find_risk_table(tabs)
        if not t:
            continue
        owner = _col(t.header, "proprietaire", "owner", "responsable du risque", "pilote")
        treat = _col(t.header, "traitement", "option", "decision", "treatment")
        rows = [r for r in t.rows if any(c for c in r[:3])]
        if owner is None:
            out.append(_check(f"RISK-OWNER-{d['filename']}", "6.1.2", "Propriétaires des risques", "ecart", NC_MIN,
                              f"Aucune colonne « propriétaire du risque » dans {d['filename']}.", [d["filename"]]))
        else:
            empty = [i + 1 for i, r in enumerate(rows) if owner >= len(r) or not r[owner].strip()]
            out.append(_check(f"RISK-OWNER-{d['filename']}", "6.1.2", "Propriétaires des risques",
                              "ecart" if empty else "ok", NC_MIN if empty else OK,
                              (f"{len(empty)} risque(s) sur {len(rows)} sans propriétaire (lignes {', '.join(map(str, empty[:15]))})."
                               if empty else f"Les {len(rows)} risques ont un propriétaire."), [d["filename"]]))
        if treat is not None:
            empty = [i + 1 for i, r in enumerate(rows) if treat >= len(r) or not r[treat].strip()]
            if empty:
                out.append(_check(f"RISK-TREAT-{d['filename']}", "6.1.3", "Option de traitement par risque", "ecart",
                                  OBS, f"{len(empty)} risque(s) sans option de traitement "
                                       f"(lignes {', '.join(map(str, empty[:15]))}).", [d["filename"]]))
    return out


# ------------------------------------------------------------ croisement SoA / risques
def check_soa_vs_risks(store: Store, soa: dict | None) -> list[dict]:
    if not soa:
        return []
    excluded = {r["id"] for r in soa["rows"] if r["applicable"] is False}
    cited: dict[str, set[str]] = {}
    pat = re.compile(r"\bA\s?\.\s?([5-8])\s?\.\s?(\d{1,2})\b")
    for d in store.documents():
        if d["error"] or d["eff_type"] not in ("plan_traitement", "registre_risques"):
            continue
        texts = [r[0] for r in store.db.execute("SELECT text FROM chunks WHERE doc_id=?", (d["id"],))]
        for m in pat.finditer("\n".join(texts)):
            cited.setdefault(f"A.{m.group(1)}.{int(m.group(2))}", set()).add(d["filename"])
    conflicts = sorted(c for c in cited if c in excluded)
    if not conflicts:
        return []
    files = sorted({f for c in conflicts for f in cited[c]})
    return [_check("CROSS-SOA-RTP", "6.1.3", "Cohérence SoA ↔ traitement des risques", "ecart", NC_MIN,
                   f"Mesure(s) exclue(s) dans la SoA mais utilisée(s) pour traiter des risques : {', '.join(conflicts)}.",
                   [soa["filename"]] + files)]


def run_all(store: Store, cfg: Config, ref: dict, today: date | None = None) -> list[dict]:
    results = check_mandatory_docs(store, ref)
    results += check_document_metadata(store, cfg, today)
    soa_checks, soa = check_soa(store)
    results += soa_checks
    results += check_risk_register(store)
    results += check_soa_vs_risks(store, soa)
    return results
