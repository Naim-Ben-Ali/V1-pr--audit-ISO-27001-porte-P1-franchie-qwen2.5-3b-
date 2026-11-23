"""Recherche hybride : plein texte (BM25/FTS5) + vecteurs, fusion par rangs (RRF)."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .llm import Extract
from .store import Store
from .text_utils import fts_query


@dataclass
class RetrievalResult:
    extracts: list[Extract]
    lexical_hits: int
    max_cosine: float

    def summary(self) -> dict:
        return {"lexical_hits": self.lexical_hits, "max_cosine": round(self.max_cosine, 3),
                "extraits": [{"id": e.eid, "doc": e.filename, "section": e.section, "score": round(e.score, 4)}
                             for e in self.extracts]}


def query_text(req: dict) -> str:
    return req["exigence"] + " " + " ".join(req.get("mots_cles", []))


def search(store: Store, req: dict, query_vec: np.ndarray | None, cfg) -> RetrievalResult:
    k_cand = int(cfg.get("retrieval.candidates", 30))
    top_k = int(cfg.get("retrieval.top_k", 6))
    boost = float(cfg.get("retrieval.type_boost", 1.5))
    rrf_k = 60

    # 1) plein texte sur les mots-clés de l'exigence
    lex = store.fts(fts_query(req.get("mots_cles", [])), k_cand)

    # 2) similarité vectorielle avec l'exigence reformulée
    mat, ids = store.embeddings()
    vec: list[tuple[int, float]] = []
    max_cos = 0.0
    if query_vec is not None and len(ids):
        sims = mat @ query_vec.astype(np.float32)
        order = np.argsort(-sims)[:k_cand]
        vec = [(ids[i], float(sims[i])) for i in order]
        max_cos = float(sims[order[0]]) if len(order) else 0.0

    # 3) fusion RRF + bonus si le document est du type attendu
    fused: dict[int, float] = {}
    for rank, (cid, _) in enumerate(lex):
        fused[cid] = fused.get(cid, 0) + 1 / (rrf_k + rank + 1)
    for rank, (cid, _) in enumerate(vec):
        fused[cid] = fused.get(cid, 0) + 1 / (rrf_k + rank + 1)
    rows = store.chunk_rows(list(fused))
    expected = set(req.get("doc_attendus", []))
    for cid, r in rows.items():
        if r["doc_type"] in expected:
            fused[cid] *= boost

    best = sorted(fused, key=fused.get, reverse=True)
    # Diversité : au plus 3 extraits du même document
    picked, per_doc = [], {}
    for cid in best:
        r = rows.get(cid)
        if r is None:
            continue
        if per_doc.get(r["doc_id"], 0) >= 3:
            continue
        per_doc[r["doc_id"]] = per_doc.get(r["doc_id"], 0) + 1
        picked.append(Extract(eid=f"E{len(picked) + 1}", chunk_id=cid, doc_id=r["doc_id"],
                              filename=r["filename"], doc_type=r["doc_type"], section=r["section"] or "",
                              page=r["page"], text=r["text"], score=fused[cid]))
        if len(picked) >= top_k:
            break
    return RetrievalResult(picked, len(lex), max_cos)
