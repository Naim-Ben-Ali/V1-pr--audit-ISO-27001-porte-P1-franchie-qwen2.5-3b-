"""Interface locale de l'assistant de pré-audit ISO 27001.

Lancement :  streamlit run app.py
L'interface écoute uniquement sur 127.0.0.1 (voir .streamlit/config.toml).
"""
from __future__ import annotations

import re
from pathlib import Path

import pandas as pd
import streamlit as st

from preaudit.analyze import run_analysis
from preaudit.classes import DOC, NC_MAJ, NC_MIN, OBS, OFI, OK, ORDER, RANK, REVIEW, STATUS_LABELS
from preaudit.config import load_config, load_referentiel
from preaudit.ingest import ingest_folder
from preaudit.llm import get_llm
from preaudit.report import export
from preaudit.store import Store

st.set_page_config(page_title="Pré-audit ISO 27001 (local)", layout="wide")

cfg = load_config()
ref = load_referentiel(cfg)
REQS = {r["id"]: r for r in ref["exigences"]}
TYPES = ref["types_documents"]
llm = get_llm(cfg)
DECISIONS = ["validé", "modifié", "rejeté"]
CHOICES = [NC_MAJ, NC_MIN, OBS, OFI, DOC]


@st.cache_resource
def open_store(path: str) -> Store:
    return Store(path)


# ------------------------------------------------------------------ barre latérale
with st.sidebar:
    st.header("Mission")
    existing = sorted(p.name for p in cfg.missions_dir.glob("*") if (p / "mission.sqlite").exists()) \
        if cfg.missions_dir.exists() else []
    choice = st.selectbox("Mission existante", ["(nouvelle)"] + existing)
    name = st.text_input("Nom de la mission (sans espaces)", value="" if choice == "(nouvelle)" else choice)
    name = re.sub(r"[^\w\-]", "_", name.strip())
    if not name:
        st.info("Donne un nom à la mission pour commencer (un client = une mission).")
        st.stop()
    store = open_store(str(cfg.missions_dir / name / "mission.sqlite"))
    org = st.text_input("Organisme audité", value=store.get_meta("org", ""))
    if org != store.get_meta("org", ""):
        store.set_meta("org", org)

    st.divider()
    ok, msg = llm.health()
    (st.success if ok else st.error)(msg)
    st.caption(f"Jugement : `{cfg.get('ollama.llm_model')}` · Embeddings : `{cfg.get('ollama.embed_model')}` · "
               f"Contexte : {cfg.get('ollama.num_ctx')} tokens")
    st.caption("Tout reste sur ce poste. Données de mission : " + str(cfg.missions_dir / name))

st.title("Assistant de pré-audit documentaire ISO/IEC 27001")
st.caption("Revue documentaire préparatoire (Stage 1). L'outil propose ; l'auditeur valide chaque constat.")

tab_docs, tab_run, tab_review, tab_report = st.tabs(["1. Documents", "2. Analyse", "3. Revue", "4. Rapport"])

# ------------------------------------------------------------------ 1. Documents
with tab_docs:
    folder = st.text_input("Dossier des documents du client",
                           value=store.get_meta("docs_folder", r"C:\ISO27001-AI\documents"))
    if st.button("Indexer / mettre à jour", type="primary", disabled=not ok):
        bar = st.progress(0.0, "Démarrage…")
        try:
            r = ingest_folder(store, folder, cfg, llm, lambda f, m: bar.progress(min(f, 1.0), m))
            st.success(f"{len(r['ajoutés'])} ajouté(s), {len(r['mis_à_jour'])} mis à jour, "
                       f"{len(r['inchangés'])} inchangé(s), {len(r['supprimés'])} supprimé(s).")
            for e in r["erreurs"]:
                st.error(e)
        except FileNotFoundError as e:
            st.error(str(e))

    docs = [dict(d) for d in store.documents()]
    if docs:
        df = pd.DataFrame([{
            "Document": d["filename"],
            "Type": TYPES.get(d["eff_type"], {}).get("libelle", d["eff_type"]) + (" ✎" if d["type_override"] else ""),
            "Version": d["version"] or "—", "Date": d["doc_date"] or "—",
            "Approbation": d["approved_by"] or "—", "Statut": d["status"], "Extraits": d["n_chunks"],
            "OCR": "oui" if d["ocr_used"] else "", "Erreur": d["error"] or ""} for d in docs])
        st.dataframe(df, width="stretch", hide_index=True)
        with st.expander("Corriger le type d'un document (le typage automatique peut se tromper)"):
            c1, c2, c3 = st.columns([3, 3, 1])
            by_id = {d["id"]: d for d in docs}
            target = by_id[c1.selectbox("Document", list(by_id), format_func=lambda i: by_id[i]["filename"])]
            keys = list(TYPES) + ["autre"]
            new_type = c2.selectbox("Type", keys, index=keys.index(target["eff_type"]) if target["eff_type"] in keys else 0,
                                    format_func=lambda k: TYPES.get(k, {}).get("libelle", k))
            if c3.button("Appliquer"):
                store.set_type_override(target["id"], new_type)
                st.rerun()
    else:
        st.info("Aucun document indexé. Indique le dossier puis clique sur « Indexer ».")

# ------------------------------------------------------------------ 2. Analyse
with tab_run:
    runs = store.runs()
    if runs:
        st.dataframe(pd.DataFrame([{"N°": r["id"], "Début": r["started_at"], "Fin": r["finished_at"] or "—",
                                    "État": r["status"], "Modèle": r["llm_model"], "Exigences": len(store.findings(r["id"]))}
                                   for r in runs]), hide_index=True, width="stretch")
    st.write(f"{len(ref['exigences'])} exigences (clauses 4 à 10) + contrôles automatiques.")
    st.caption("Astuce : pour une analyse longue, lance plutôt `python -m preaudit analyze --mission "
               f"{name}` dans un terminal ; elle reprend où elle s'est arrêtée avec `--resume`.")
    c1, c2 = st.columns(2)
    unfinished = [r for r in runs if r["status"] != "terminé"]
    start_new = c1.button("Lancer une nouvelle analyse", type="primary", disabled=not ok or not docs)
    resume = c2.button(f"Reprendre l'analyse n° {unfinished[0]['id']}", disabled=not ok) if unfinished else False
    if start_new or resume:
        bar = st.progress(0.0, "Démarrage…")
        try:
            rid = run_analysis(store, cfg, llm, run_id=unfinished[0]["id"] if resume else None,
                               progress=lambda f, m: bar.progress(min(f, 1.0), m))
            st.success(f"Analyse n° {rid} terminée. Passe à l'onglet Revue.")
        except RuntimeError as e:
            st.error(str(e))

# ------------------------------------------------------------------ 3. Revue
with tab_review:
    runs = store.runs()
    if not runs:
        st.info("Lance d'abord une analyse.")
    else:
        rid = st.selectbox("Analyse", [r["id"] for r in runs], format_func=lambda i: f"n° {i}", key="rev_run")
        decisions = store.decisions(rid)
        items = []
        for f in store.findings(rid):
            items.append(dict(kind="finding", id=f["req_id"], cls=f["classification"], source=f["source"],
                              title=REQS.get(f["req_id"], {}).get("exigence", ""), data=f))
        for c in store.checks(rid):
            if c["status"] != "ok":
                items.append(dict(kind="check", id=c["check_id"], cls=c["classification"], source=c["source"],
                                  title=c["title"], data=c))
        c1, c2, c3 = st.columns([3, 1, 1])
        show = c1.multiselect("Classements", ORDER, default=[NC_MAJ, NC_MIN, REVIEW, OBS])
        only_open = c2.checkbox("Non revus seulement", value=True)
        n_done = sum(1 for i in items if (i["kind"], i["id"]) in decisions)
        c3.metric("Revus", f"{n_done} / {len(items)}")
        conf_rank = {"Faible": 0, "Moyenne": 1, "Haute": 2}
        items = [i for i in items if i["cls"] in show and not (only_open and (i["kind"], i["id"]) in decisions)]
        # Priorité : classement, puis confiance la plus faible d'abord
        items.sort(key=lambda i: (RANK.get(i["cls"], 99), conf_rank.get(i["data"].get("confidence", ""), 1)))

        for it in items:
            d = it["data"]
            dec = decisions.get((it["kind"], it["id"]))
            badge = f" · {dec['decision']}" if dec else ""
            with st.expander(f"**{it['cls']}** · {it['id']} — {it['title'][:110]}{badge}"):
                if it["kind"] == "finding":
                    st.markdown(f"**Statut :** {STATUS_LABELS.get(d['status'], d['status'])} · "
                                f"**Confiance (calculée) :** {d['confidence']} · **Méthode :** {d['method']}")
                    if d["rationale"]:
                        st.markdown(f"**Analyse :** {d['rationale']}")
                    for c in d["citations"]:
                        loc = ", ".join(x for x in [c.get("section") or "", f"p. {c['page']}" if c.get("page") else ""] if x)
                        st.success(f"« {c['texte']} »\n\n— {c['document']}{' · ' + loc if loc else ''}  ✔ vérifiée mot à mot")
                    for c in d["rejected_citations"]:
                        st.warning(f"Citation écartée ({c.get('motif')}) : « {c.get('texte', '')[:300]} »")
                    if d["missing"]:
                        st.markdown("**Éléments manquants :** " + " ; ".join(d["missing"]))
                    if d["questions"]:
                        st.markdown("**Questions pour l'audit sur site :** " + " / ".join(d["questions"]))
                    if d["error"]:
                        st.error(d["error"])
                    with st.popover("Extraits consultés"):
                        for e in d["retrieval"].get("extraits", []):
                            st.caption(f"{e['id']} · {e['doc']} · {e['section']} · score {e['score']}")
                else:
                    st.markdown(f"**Contrôle automatique ({d['source']}) :** {d['detail']}")
                    if d["evidence"]:
                        st.caption("Documents : " + ", ".join(d["evidence"]))
                with st.form(f"f_{it['kind']}_{it['id']}"):
                    a, b = st.columns(2)
                    decision = a.radio("Décision", DECISIONS, horizontal=True,
                                       index=DECISIONS.index(dec["decision"]) if dec else 0)
                    current = (dec or {}).get("classification_override") or it["cls"]
                    override = b.selectbox("Classement (si modifié)", CHOICES,
                                           index=CHOICES.index(current) if current in CHOICES else 0)
                    comment = st.text_area("Commentaire de l'auditeur", value=(dec or {}).get("comment", ""))
                    if st.form_submit_button("Enregistrer"):
                        if decision == "rejeté" and not comment.strip():
                            st.error("Un rejet doit être justifié en commentaire.")
                        else:
                            store.decide(rid, it["kind"], it["id"], decision,
                                         override if decision == "modifié" else None, comment.strip())
                            st.rerun()
        if not items:
            st.success("Rien à revoir avec ces filtres.")

# ------------------------------------------------------------------ 4. Rapport
with tab_report:
    runs = store.runs()
    if not runs:
        st.info("Lance d'abord une analyse.")
    else:
        rid = st.selectbox("Analyse", [r["id"] for r in runs], format_func=lambda i: f"n° {i}", key="rep_run")
        final = st.checkbox("Rapport final : uniquement les constats revus par l'auditeur", value=False)
        if st.button("Générer le rapport", type="primary"):
            paths = export(store, cfg, rid, org or name, cfg.missions_dir / name / "rapports",
                           include_unreviewed=not final)
            st.session_state["report_paths"] = {k: str(v) for k, v in paths.items()}
            if "docx" not in paths:
                st.warning("Pandoc n'est pas installé : seul le Markdown est généré (voir README).")
        for kind, p in st.session_state.get("report_paths", {}).items():
            data = Path(p).read_bytes()
            st.download_button(f"Télécharger le rapport ({kind.upper()})", data, file_name=Path(p).name,
                               mime="text/markdown" if kind == "md" else
                               "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
