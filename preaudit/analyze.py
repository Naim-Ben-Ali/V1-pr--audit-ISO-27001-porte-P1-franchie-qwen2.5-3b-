"""Moteur d'analyse : plan → tri sans LLM → jugement par exigence → vérification
des citations → classement par règles. Chaque résultat est enregistré dès qu'il
existe : une analyse interrompue reprend là où elle s'est arrêtée.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Callable

from . import checks
from .classes import classify_requirement
from .config import Config, load_referentiel
from .llm import Extract
from .retrieve import query_text, search
from .store import Store
from .text_utils import loose, normalize

log = logging.getLogger(__name__)

MIN_CITATION_CHARS = 20


# ------------------------------------------------------------ vérification des citations
def verify_citations(citations: list[dict], extracts: list[Extract]) -> tuple[list[dict], list[dict]]:
    """Une citation est retenue seulement si on la retrouve mot pour mot dans l'extrait cité
    (ou, à défaut, dans un autre extrait fourni). Comparaison tolérante sur la casse,
    les espaces, les guillemets ; puis sans accents ni ponctuation."""
    by_id = {e.eid: e for e in extracts}
    ok, rejected = [], []
    for c in citations:
        quote = (c.get("texte") or "").strip().strip('"«»“” ')
        if len(quote) < MIN_CITATION_CHARS:
            rejected.append({**c, "motif": "citation trop courte"})
            continue
        order = [by_id[c["extrait_id"]]] if c.get("extrait_id") in by_id else []
        order += [e for e in extracts if e not in order]
        match = None
        for e in order:
            if normalize(quote) in normalize(e.text) or loose(quote) in loose(e.text):
                match = e
                break
        if match is None:
            rejected.append({**c, "motif": "introuvable dans les extraits"})
            continue
        ok.append({"texte": quote, "document": match.filename, "section": match.section,
                   "page": match.page, "extrait_id": match.eid})
    return ok, rejected


def confidence(status: str, n_verified: int, n_rejected: int, retrieval) -> str:
    """Confiance CALCULÉE à partir de faits, jamais auto-déclarée par le modèle."""
    if status == "non_trouve":
        return "Haute" if retrieval.lexical_hits == 0 else "Moyenne"
    if status == "a_revoir" or n_rejected > n_verified:
        return "Faible"
    if status == "documente" and n_verified >= 2 and n_rejected == 0:
        return "Haute"
    if n_verified >= 1:
        return "Moyenne"
    return "Faible"


# ------------------------------------------------------------ une exigence
def assess(req: dict, store: Store, llm, cfg: Config, qvec) -> dict:
    t0 = time.time()
    res = search(store, req, qvec, cfg)
    base = dict(req_id=req["id"], source=req["source"], retrieval=res.summary(), error=None)

    # 1) Tri sans LLM : ni mot-clé ni proximité sémantique → non trouvé
    if res.lexical_hits == 0 and res.max_cosine < float(cfg.get("retrieval.triage_min_cosine", 0.45)) \
            or not res.extracts:
        st = "non_trouve"
        return {**base, "status": st, "classification": classify_requirement(st, req.get("critique", False)),
                "confidence": confidence(st, 0, 0, res), "method": "tri",
                "rationale": "Aucun passage pertinent trouvé par la recherche (mots-clés et similarité).",
                "missing": [], "citations": [], "rejected_citations": [],
                "questions": req.get("questions_audit", []), "duration_s": round(time.time() - t0, 2)}

    # 2) Jugement par le modèle
    try:
        j = llm.judge(req, res.extracts)
    except Exception as e:
        log.warning("%s : %s", req["id"], e)
        return {**base, "status": "erreur", "classification": classify_requirement("erreur", False),
                "confidence": "Faible", "method": "llm", "rationale": "", "missing": [], "citations": [],
                "rejected_citations": [], "questions": req.get("questions_audit", []), "error": str(e),
                "duration_s": round(time.time() - t0, 2)}

    # 3) Vérification mot à mot des citations
    verified, rejected = verify_citations(j["citations"], res.extracts)
    if j["statut"] == "absent":
        st = "non_trouve"
    elif not verified:
        st = "a_revoir"          # règle d'or : rien de favorable sans citation vérifiée
    else:
        st = "documente" if j["statut"] == "documente" else "partiel"

    questions = list(dict.fromkeys(j["questions_audit"] + req.get("questions_audit", [])))[:3]
    return {**base, "status": st, "classification": classify_requirement(st, req.get("critique", False)),
            "confidence": confidence(st, len(verified), len(rejected), res), "method": "llm",
            "rationale": j["justification"], "missing": j["elements_manquants"],
            "citations": verified, "rejected_citations": rejected, "questions": questions,
            "duration_s": round(time.time() - t0, 2)}


# ------------------------------------------------------------ analyse complète
def run_analysis(store: Store, cfg: Config, llm, run_id: int | None = None,
                 req_filter: Callable[[dict], bool] | None = None,
                 progress: Callable[[float, str], None] | None = None) -> int:
    ref = load_referentiel(cfg)
    reqs = [r for r in ref["exigences"] if not req_filter or req_filter(r)]
    if run_id is None:
        run_id = store.new_run(llm_model=llm.model, embed_model=getattr(llm, "embed_model", ""),
                               backend=cfg.backend, referentiel_version=ref["version"],
                               corpus_fingerprint=store.corpus_fingerprint(),
                               params=json.dumps({"retrieval": cfg.get("retrieval"),
                                                  "num_ctx": cfg.get("ollama.num_ctx"),
                                                  "temperature": cfg.get("ollama.temperature")}))
    else:
        run = store.run(run_id)
        if run["corpus_fingerprint"] != store.corpus_fingerprint():
            raise RuntimeError("Les documents ont changé depuis le début de cette analyse : lance une nouvelle analyse.")

    # Phase A — contrôles déterministes (quasi gratuits)
    if progress:
        progress(0.0, "Contrôles automatiques (documents obligatoires, SoA, métadonnées)")
    for c in checks.run_all(store, cfg, ref):
        store.save_check(run_id, c)

    # Phase B — embeddings des exigences, puis libération du modèle d'embedding
    todo = [r for r in reqs if r["id"] not in store.done_req_ids(run_id)]
    qvecs = llm.embed([query_text(r) for r in todo]) if todo else []
    llm.unload(getattr(llm, "embed_model", ""))

    # Phase C — une exigence à la fois, résultat enregistré immédiatement
    for i, req in enumerate(todo):
        if progress:
            progress((i + 1) / (len(todo) + 1), f"{req['id']} — {req['exigence'][:70]}…")
        store.save_finding(run_id, assess(req, store, llm, cfg, qvecs[i]))

    store.finish_run(run_id, "terminé")
    if progress:
        progress(1.0, "Analyse terminée")
    return run_id
