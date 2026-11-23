"""Ligne de commande.

  python -m preaudit check
  python -m preaudit ingest  --mission medina --docs "C:\\ISO27001-AI\\documents"
  python -m preaudit analyze --mission medina [--resume 3] [--only 9.2,10.2]
  python -m preaudit report  --mission medina [--run 3] [--org "Médina Logistique"] [--final]
  python -m preaudit eval    [--corpus eval/corpus_test]

L'analyse en ligne de commande est conseillée pour les longues exécutions (de nuit) :
elle est reprenable avec --resume si elle est interrompue.
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

from .analyze import run_analysis
from .config import load_config
from .ingest import ingest_folder
from .llm import get_llm
from .report import export
from .store import Store


def mission_store(cfg, name: str) -> Store:
    return Store(cfg.missions_dir / name / "mission.sqlite")


def _progress(frac: float, msg: str) -> None:
    sys.stdout.write(f"\r[{frac * 100:5.1f} %] {msg[:90]:<90}")
    sys.stdout.flush()
    if frac >= 1:
        print()


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):      # console Windows : jamais d'erreur sur « é », « ✔ »…
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(prog="preaudit", description="Pré-audit documentaire ISO 27001 local")
    ap.add_argument("--config", default=None)
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="vérifie qu'Ollama et les modèles sont disponibles")
    p = sub.add_parser("ingest")
    p.add_argument("--mission", required=True)
    p.add_argument("--docs", required=True)
    p = sub.add_parser("analyze")
    p.add_argument("--mission", required=True)
    p.add_argument("--resume", type=int)
    p.add_argument("--only", help="liste de clauses, ex. 9.2,10.2")
    p = sub.add_parser("report")
    p.add_argument("--mission", required=True)
    p.add_argument("--run", type=int)
    p.add_argument("--org", default="")
    p.add_argument("--final", action="store_true", help="n'inclure que les constats revus par l'auditeur")
    p = sub.add_parser("eval")
    p.add_argument("--corpus", default=None)
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format="%(levelname)s %(message)s")
    cfg = load_config(args.config)
    llm = get_llm(cfg)

    if args.cmd == "check":
        ok, msg = llm.health()
        print(("OK  " if ok else "ERREUR  ") + msg)
        return 0 if ok else 1

    if args.cmd == "eval":
        from eval.evaluate import run_eval
        return run_eval(cfg, llm, Path(args.corpus) if args.corpus else None)

    ok, msg = llm.health()
    if not ok:
        print("ERREUR  " + msg)
        return 1
    store = mission_store(cfg, args.mission)

    if args.cmd == "ingest":
        t0 = time.time()
        r = ingest_folder(store, args.docs, cfg, llm, _progress)
        for k, v in r.items():
            print(f"{k:12} {len(v)}" + (f"  {v}" if k == "erreurs" and v else ""))
        print(f"Durée : {time.time() - t0:.0f} s")
    elif args.cmd == "analyze":
        req_filter = None
        if args.only:
            wanted = [s.strip() for s in args.only.split(",")]
            req_filter = lambda r: any(r["source"] == w or r["source"].startswith(w + ".") for w in wanted)
        t0 = time.time()
        rid = run_analysis(store, cfg, llm, run_id=args.resume, req_filter=req_filter, progress=_progress)
        n = len(store.findings(rid))
        print(f"Analyse n° {rid} : {n} exigence(s) en {time.time() - t0:.0f} s "
              f"({(time.time() - t0) / max(n, 1):.1f} s par exigence)")
    elif args.cmd == "report":
        rid = args.run or (store.runs()[0]["id"] if store.runs() else None)
        if rid is None:
            print("Aucune analyse dans cette mission.")
            return 1
        org = args.org or store.get_meta("org", args.mission)
        paths = export(store, cfg, rid, org, cfg.missions_dir / args.mission / "rapports",
                       include_unreviewed=not args.final)
        for k, v in paths.items():
            print(f"{k.upper():5} {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
