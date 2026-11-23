"""Ingestion incrémentale d'un dossier de documents dans la base de la mission."""
from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path
from typing import Callable

from .classify import classify, extract_metadata
from .config import Config, load_referentiel
from .parsers import SUPPORTED, chunk_blocks, parse
from .store import Store, now

EXCLUDED_TYPE = "norme_exclue"


def looks_like_iso_standard(parsed) -> bool:
    """Le texte de la norme elle-même ne doit jamais servir de « preuve » : on le reconnaît
    à ses en-têtes répétés « ISO/IEC 270xx » et à sa mention de droits réservés."""
    text = "\n".join(b.text + " " + b.section for b in parsed.blocks[:200])
    heads = len(re.findall(r"ISO\s*/\s*IEC\s*2700[0-9]\s*:\s*20\d\d", text))
    rights = re.search(r"©\s*ISO|Tous droits réservés|All rights reserved", text, re.I)
    normative = re.search(r"Annexe?\s*A\s*\(normative\)|Termes et définitions|Terms and definitions", text, re.I)
    return heads >= 3 and bool(rights) and bool(normative)

log = logging.getLogger(__name__)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def list_files(folder: Path) -> list[Path]:
    return sorted(p for p in folder.rglob("*")
                  if p.is_file() and p.suffix.lower() in SUPPORTED and not p.name.startswith(("~$", ".")))


def ingest_folder(store: Store, folder: str | Path, cfg: Config, llm,
                  progress: Callable[[float, str], None] | None = None) -> dict:
    folder = Path(folder).resolve()
    if not folder.is_dir():
        raise FileNotFoundError(f"Dossier introuvable : {folder}")
    types = load_referentiel(cfg)["types_documents"]
    max_chars = int(cfg.get("chunking.max_chars", 1400))
    min_chars = int(cfg.get("chunking.min_chars", 200))
    files = list_files(folder)
    report = {"ajoutés": [], "mis_à_jour": [], "inchangés": [], "supprimés": [], "exclus": [], "erreurs": []}

    # Documents retirés du dossier depuis la dernière ingestion
    current = {str(p) for p in files}
    for d in store.documents():
        if d["path"] not in current:
            store.delete_doc(d["id"])
            report["supprimés"].append(d["filename"])

    for i, path in enumerate(files):
        if progress:
            progress(i / max(len(files), 1), f"Lecture de {path.name}")
        digest = sha256(path)
        old = store.doc_by_path(str(path))
        if old and old["sha256"] == digest and not old["error"]:
            report["inchangés"].append(path.name)
            continue
        override = old["type_override"] if old else None
        if old:
            store.delete_doc(old["id"])
        try:
            # 1er passage sans OCR (rapide) : suffit pour reconnaître le texte de la norme
            parsed = parse(path, ocr=False)
            if looks_like_iso_standard(parsed):
                store.add_doc(dict(path=str(path), filename=path.name, sha256=digest, doc_type=EXCLUDED_TYPE,
                                   type_override=None, n_chunks=0, pages=parsed.pages, ingested_at=now(),
                                   error=None), [], [])
                report["exclus"].append(f"{path.name} : texte de la norme ISO, exclu du corpus de preuves")
                continue
            if parsed.ocr_pending:
                if progress:
                    progress(i / max(len(files), 1), f"OCR de {parsed.ocr_pending} page(s) : {path.name}")
                parsed = parse(path, ocr=True)
            doc_type, score = classify(path.name, parsed, types)
            meta = extract_metadata(parsed)
            chunks = chunk_blocks(parsed.blocks, max_chars, min_chars)
            texts = [(c.section + "\n" if c.section else "") + c.text for c in chunks]
            embs = llm.embed(texts) if texts else []
            rec = dict(path=str(path), filename=path.name, sha256=digest, doc_type=doc_type,
                       type_score=score, type_override=override, version=meta["version"],
                       approved_by=meta["approved_by"], doc_date=meta["doc_date"],
                       review_date=meta["review_date"], status=meta["status"], pages=parsed.pages,
                       ocr_used=int(parsed.ocr_used), n_chunks=len(chunks), ingested_at=now(), error=None)
            store.add_doc(rec, [(c.section, c.page, c.text, embs[j]) for j, c in enumerate(chunks)],
                          [(t.name, t.header, t.rows) for t in parsed.tables])
            report["mis_à_jour" if old else "ajoutés"].append(path.name)
            if not chunks:
                log.warning("%s : aucun texte extrait (scan sans OCR ?)", path.name)
        except Exception as e:
            log.exception("Échec d'ingestion : %s", path)
            store.add_doc(dict(path=str(path), filename=path.name, sha256=digest, doc_type="erreur",
                               type_override=override, n_chunks=0, ingested_at=now(), error=str(e)), [], [])
            report["erreurs"].append(f"{path.name} : {e}")

    store.set_meta("docs_folder", str(folder))
    store.set_meta("embed_model", getattr(llm, "embed_model", ""))
    llm.unload(getattr(llm, "embed_model", ""))   # libère la VRAM avant la phase d'analyse
    if progress:
        progress(1.0, "Ingestion terminée")
    return report
