"""Évaluation sur le corpus à écarts plantés (porte P1 de la feuille de route).

  python -m preaudit eval            (génère le corpus si besoin, l'analyse, compare à la vérité terrain)

Indicateurs :
  - faux « documenté »      : écart planté déclaré documenté (erreur la plus grave, cible 0)
  - rappel des écarts       : part des écarts plantés détectés
  - exigences bien trouvées : part des exigences documentées reconnues comme telles
  - citations valides       : citations vérifiées / (vérifiées + écartées)
  - temps moyen par appel LLM
"""
from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path

import yaml

from preaudit.analyze import run_analysis
from preaudit.ingest import ingest_folder
from preaudit.store import Store

HERE = Path(__file__).parent


def run_eval(cfg, llm, corpus: Path | None = None) -> int:
    corpus = corpus or HERE / "corpus_test"
    if not corpus.exists():
        from eval.make_test_corpus import build
        build(corpus)
    truth = yaml.safe_load((HERE / "verite_terrain.yaml").read_text(encoding="utf-8"))

    ok, msg = llm.health()
    if not ok:
        print("ERREUR  " + msg)
        return 1
    work = cfg.missions_dir / "_evaluation"
    shutil.rmtree(work, ignore_errors=True)
    store = Store(work / "mission.sqlite")
    ingest_folder(store, corpus, cfg, llm)
    rid = run_analysis(store, cfg, llm)

    checks = {c["check_id"]: c for c in store.checks(rid)}
    findings = {f["req_id"]: f for f in store.findings(rid)}
    lines, false_doc, gaps_total, gaps_found, doc_total, doc_found = [], [], 0, 0, 0, 0

    for cid, expected in truth["controles"].items():
        c = checks.get(cid)
        got = "absent" if c is None else ("detecte" if c["status"] in ("ecart", "a_confirmer") else "ok")
        if expected == "detecte":
            gaps_total += 1
            gaps_found += got == "detecte"
        lines.append((cid, expected, got, "✔" if got == expected else "✘"))

    for rid_, expected in truth["exigences"].items():
        f = findings.get(rid_)
        got = f["status"] if f else "absent"
        if expected in ("documente", "documente_ou_partiel"):
            doc_total += 1
            good = got == "documente" or (expected == "documente_ou_partiel" and got == "partiel")
            doc_found += good
        else:
            gaps_total += 1
            good = got != "documente"
            gaps_found += good
            if not good:
                false_doc.append(rid_)
        lines.append((rid_, expected, got, "✔" if good else "✘"))

    verified = sum(len(f["citations"]) for f in findings.values())
    rejected = sum(len(f["rejected_citations"]) for f in findings.values())
    llm_times = [f["duration_s"] for f in findings.values() if f["method"] == "llm" and f["duration_s"] is not None]

    print(f"\n{'Élément':42} {'Attendu':14} {'Obtenu':14}")
    for name, exp, got, mark in lines:
        print(f"{mark} {name:40} {exp:14} {got:14}")
    res = {
        "date": datetime.now().isoformat(timespec="seconds"),
        "modele": llm.model, "backend": cfg.backend,
        "faux_documente": false_doc,
        "rappel_ecarts": f"{gaps_found}/{gaps_total}",
        "exigences_documentees_trouvees": f"{doc_found}/{doc_total}",
        "citations_retenues_verifiees": f"{verified}/{verified}",
        "citations_ecartees_par_la_verification": f"{rejected}/{verified + rejected}",
        "tri_sans_llm": sum(1 for f in findings.values() if f["method"] == "tri"),
        "appels_llm": len(llm_times),
        "temps_moyen_par_appel_s": round(sum(llm_times) / len(llm_times), 1) if llm_times else 0,
    }
    print("\n" + json.dumps(res, ensure_ascii=False, indent=2))
    # Porte P1 : aucun faux « documenté ». Les citations retenues sont vérifiées par construction ;
    # les citations écartées mesurent combien le modèle reformule ou invente (bloqué par la vérification).
    porte = not false_doc
    taux = rejected / max(verified + rejected, 1)
    print("\nPorte P1 :", "FRANCHIE (aucun faux « documenté »)" if porte else
          f"NON FRANCHIE (faux « documenté » : {', '.join(false_doc)})")
    print(f"Citations écartées par la vérification : {rejected} ({taux:.0%})"
          + ("  → le modèle reformule beaucoup : envisager un modèle plus grand" if taux > 0.15 else "  → acceptable"))
    hist = HERE / "historique_evaluations.jsonl"
    with open(hist, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(res, ensure_ascii=False) + "\n")
    return 0
