"""Stockage d'une mission : un seul fichier SQLite (texte, index, résultats, décisions).

Un fichier = une mission = un client : facile à chiffrer, sauvegarder, détruire.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

import numpy as np

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS documents(
  id INTEGER PRIMARY KEY, path TEXT UNIQUE, filename TEXT, sha256 TEXT,
  doc_type TEXT, type_score REAL, type_override TEXT,
  version TEXT, approved_by TEXT, doc_date TEXT, review_date TEXT, status TEXT,
  pages INTEGER, ocr_used INTEGER, n_chunks INTEGER, ingested_at TEXT, error TEXT);
CREATE TABLE IF NOT EXISTS chunks(
  id INTEGER PRIMARY KEY, doc_id INTEGER REFERENCES documents(id) ON DELETE CASCADE,
  ord INTEGER, section TEXT, page INTEGER, text TEXT, embedding BLOB);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
  text, section, content='chunks', content_rowid='id', tokenize='unicode61 remove_diacritics 2');
CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
  INSERT INTO chunks_fts(rowid, text, section) VALUES (new.id, new.text, new.section); END;
CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
  INSERT INTO chunks_fts(chunks_fts, rowid, text, section) VALUES ('delete', old.id, old.text, old.section); END;
CREATE TABLE IF NOT EXISTS doc_tables(
  id INTEGER PRIMARY KEY, doc_id INTEGER REFERENCES documents(id) ON DELETE CASCADE,
  name TEXT, header TEXT, rows TEXT);
CREATE TABLE IF NOT EXISTS runs(
  id INTEGER PRIMARY KEY, started_at TEXT, finished_at TEXT, status TEXT,
  llm_model TEXT, embed_model TEXT, backend TEXT, referentiel_version TEXT,
  corpus_fingerprint TEXT, params TEXT);
CREATE TABLE IF NOT EXISTS findings(
  id INTEGER PRIMARY KEY, run_id INTEGER, req_id TEXT, source TEXT,
  status TEXT, classification TEXT, confidence TEXT, method TEXT,
  rationale TEXT, missing TEXT, citations TEXT, rejected_citations TEXT, questions TEXT,
  retrieval TEXT, error TEXT, duration_s REAL, created_at TEXT, UNIQUE(run_id, req_id));
CREATE TABLE IF NOT EXISTS checks(
  id INTEGER PRIMARY KEY, run_id INTEGER, check_id TEXT, source TEXT, title TEXT,
  status TEXT, classification TEXT, detail TEXT, evidence TEXT, created_at TEXT,
  UNIQUE(run_id, check_id));
CREATE TABLE IF NOT EXISTS decisions(
  run_id INTEGER, item_kind TEXT, item_id TEXT, decision TEXT,
  classification_override TEXT, comment TEXT, decided_at TEXT,
  PRIMARY KEY(run_id, item_kind, item_id));
"""


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript(SCHEMA)
        self._emb_cache: tuple[np.ndarray, list[int]] | None = None

    # ------------------------------------------------------------ meta
    def set_meta(self, key: str, value: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (key, value))
        self.db.commit()

    def get_meta(self, key: str, default: str = "") -> str:
        r = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return r[0] if r else default

    # ------------------------------------------------------------ documents
    def doc_by_path(self, path: str):
        return self.db.execute("SELECT * FROM documents WHERE path=?", (path,)).fetchone()

    def delete_doc(self, doc_id: int) -> None:
        self.db.execute("DELETE FROM chunks WHERE doc_id=?", (doc_id,))
        self.db.execute("DELETE FROM doc_tables WHERE doc_id=?", (doc_id,))
        self.db.execute("DELETE FROM documents WHERE id=?", (doc_id,))
        self.db.commit()
        self._emb_cache = None

    def add_doc(self, rec: dict, chunks: list[tuple[str, int | None, str, np.ndarray | None]],
                tables: list[tuple[str, list[str], list[list[str]]]]) -> int:
        cols = ",".join(rec)
        cur = self.db.execute(
            f"INSERT INTO documents({cols}) VALUES({','.join('?' * len(rec))})", list(rec.values()))
        doc_id = cur.lastrowid
        for i, (section, page, text, emb) in enumerate(chunks):
            blob = emb.astype(np.float32).tobytes() if emb is not None else None
            self.db.execute("INSERT INTO chunks(doc_id,ord,section,page,text,embedding) VALUES(?,?,?,?,?,?)",
                            (doc_id, i, section, page, text, blob))
        for name, header, rows in tables:
            self.db.execute("INSERT INTO doc_tables(doc_id,name,header,rows) VALUES(?,?,?,?)",
                            (doc_id, name, json.dumps(header, ensure_ascii=False), json.dumps(rows, ensure_ascii=False)))
        self.db.commit()
        self._emb_cache = None
        return doc_id

    def documents(self) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT *, COALESCE(type_override, doc_type) AS eff_type FROM documents ORDER BY filename").fetchall()

    def set_type_override(self, doc_id: int, doc_type: str | None) -> None:
        self.db.execute("UPDATE documents SET type_override=? WHERE id=?", (doc_type, doc_id))
        self.db.commit()

    def tables(self, doc_id: int | None = None):
        q = "SELECT t.*, d.filename FROM doc_tables t JOIN documents d ON d.id=t.doc_id"
        rows = self.db.execute(q + (" WHERE doc_id=?" if doc_id else ""), (doc_id,) if doc_id else ()).fetchall()
        return [dict(r, header=json.loads(r["header"]), rows=json.loads(r["rows"])) for r in rows]

    def corpus_fingerprint(self) -> str:
        import hashlib
        h = hashlib.sha256()
        for r in self.db.execute("SELECT filename, sha256, COALESCE(type_override,doc_type) FROM documents ORDER BY filename"):
            h.update("|".join(map(str, r)).encode())
        return h.hexdigest()[:16]

    # ------------------------------------------------------------ recherche
    def embeddings(self) -> tuple[np.ndarray, list[int]]:
        if self._emb_cache is None:
            rows = self.db.execute("SELECT id, embedding FROM chunks WHERE embedding IS NOT NULL").fetchall()
            ids = [r["id"] for r in rows]
            mat = np.vstack([np.frombuffer(r["embedding"], dtype=np.float32) for r in rows]) if rows else np.zeros((0, 1))
            self._emb_cache = (mat, ids)
        return self._emb_cache

    def chunk_rows(self, ids: list[int]) -> dict[int, sqlite3.Row]:
        if not ids:
            return {}
        q = ("SELECT c.id, c.doc_id, c.section, c.page, c.text, d.filename, "
             "COALESCE(d.type_override, d.doc_type) AS doc_type, d.status AS doc_status "
             f"FROM chunks c JOIN documents d ON d.id=c.doc_id WHERE c.id IN ({','.join('?' * len(ids))})")
        return {r["id"]: r for r in self.db.execute(q, ids)}

    def fts(self, query: str, limit: int) -> list[tuple[int, float]]:
        if not query:
            return []
        try:
            rows = self.db.execute(
                "SELECT rowid, bm25(chunks_fts) AS s FROM chunks_fts WHERE chunks_fts MATCH ? ORDER BY s LIMIT ?",
                (query, limit)).fetchall()
        except sqlite3.OperationalError:
            return []
        return [(r[0], -r[1]) for r in rows]

    # ------------------------------------------------------------ analyses
    def new_run(self, **fields) -> int:
        fields.setdefault("started_at", now())
        fields.setdefault("status", "en_cours")
        cols = ",".join(fields)
        cur = self.db.execute(f"INSERT INTO runs({cols}) VALUES({','.join('?' * len(fields))})", list(fields.values()))
        self.db.commit()
        return cur.lastrowid

    def runs(self):
        return self.db.execute("SELECT * FROM runs ORDER BY id DESC").fetchall()

    def run(self, run_id: int):
        return self.db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()

    def finish_run(self, run_id: int, status: str = "terminé") -> None:
        self.db.execute("UPDATE runs SET finished_at=?, status=? WHERE id=?", (now(), status, run_id))
        self.db.commit()

    def done_req_ids(self, run_id: int) -> set[str]:
        return {r[0] for r in self.db.execute("SELECT req_id FROM findings WHERE run_id=? AND error IS NULL", (run_id,))}

    def save_finding(self, run_id: int, f: dict) -> None:
        f = {k: (json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else v) for k, v in f.items()}
        f.update(run_id=run_id, created_at=now())
        cols = ",".join(f)
        self.db.execute(f"INSERT OR REPLACE INTO findings({cols}) VALUES({','.join('?' * len(f))})", list(f.values()))
        self.db.commit()

    def save_check(self, run_id: int, c: dict) -> None:
        c = {k: (json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else v) for k, v in c.items()}
        c.update(run_id=run_id, created_at=now())
        cols = ",".join(c)
        self.db.execute(f"INSERT OR REPLACE INTO checks({cols}) VALUES({','.join('?' * len(c))})", list(c.values()))
        self.db.commit()

    def findings(self, run_id: int) -> list[dict]:
        out = []
        for r in self.db.execute("SELECT * FROM findings WHERE run_id=? ORDER BY id", (run_id,)):
            d = dict(r)
            for k in ("citations", "rejected_citations", "questions", "missing", "retrieval"):
                d[k] = json.loads(d[k]) if d.get(k) else []
            out.append(d)
        return out

    def checks(self, run_id: int) -> list[dict]:
        out = []
        for r in self.db.execute("SELECT * FROM checks WHERE run_id=? ORDER BY id", (run_id,)):
            d = dict(r)
            d["evidence"] = json.loads(d["evidence"]) if d.get("evidence") else []
            out.append(d)
        return out

    def decide(self, run_id: int, kind: str, item_id: str, decision: str,
               classification_override: str | None = None, comment: str = "") -> None:
        self.db.execute("INSERT OR REPLACE INTO decisions VALUES(?,?,?,?,?,?,?)",
                        (run_id, kind, item_id, decision, classification_override, comment, now()))
        self.db.commit()

    def decisions(self, run_id: int) -> dict[tuple[str, str], dict]:
        return {(r["item_kind"], r["item_id"]): dict(r)
                for r in self.db.execute("SELECT * FROM decisions WHERE run_id=?", (run_id,))}
