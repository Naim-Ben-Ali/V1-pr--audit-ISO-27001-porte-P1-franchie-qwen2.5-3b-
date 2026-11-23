# Assistant local de pré-audit documentaire ISO/IEC 27001 — V1

Revue documentaire préparatoire (type « Stage 1 ») d'un SMSI, **100 % locale** : aucun document ne quitte le poste.
L'outil propose des constats, l'auditeur valide chacun d'eux, et le rapport est généré par gabarit.

> **Limite assumée :** l'outil vérifie ce qui est *documenté*, pas ce qui est *appliqué*. « Non trouvé » veut dire
> « non trouvé dans les documents fournis ». La conclusion d'audit reste celle de l'auditeur.

## Ce que fait la V1

| Étape | Ce qui se passe | LLM ? |
|---|---|---|
| Ingestion | PDF, DOCX, PPTX, XLSX, CSV, TXT/MD ; OCR des pages scannées ; typage des documents ; version, date, approbation ; découpage par sections ; réindexation seulement des fichiers modifiés | non (embeddings seulement) |
| Contrôles automatiques | 14 informations documentées obligatoires ; maîtrise documentaire (version, approbation, revue dépassée) ; SoA (93 mesures, exclusions non justifiées, mesures non mises en œuvre) ; registre des risques (propriétaires) ; incohérence SoA ↔ plan de traitement | non |
| Analyse par exigence | 70 exigences atomiques des clauses 4 à 10 ; tri sans LLM quand rien n'est trouvé ; jugement JSON par le modèle local | oui |
| Garde-fous | chaque citation est retrouvée **mot pour mot** dans la source ; un verdict favorable sans citation vérifiée devient « À revoir » ; confiance calculée, pas auto-déclarée ; le texte de la norme est exclu s'il est déposé parmi les documents | — |
| Revue | valider / modifier / rejeter chaque constat (rejet justifié obligatoire) | — |
| Rapport | Markdown + DOCX (Pandoc local) : synthèse, NC potentielles, observations, contrôles, questions pour le Stage 2, matrice de traçabilité | non |

## Installation (Windows, PowerShell)

**1. Ollama** — installer depuis le site officiel d'Ollama, puis choisir le modèle selon la VRAM de la carte
(Gestionnaire des tâches › Performance › GPU › « Mémoire GPU dédiée ») :

| VRAM | Modèle de jugement | Dans `config.yaml` |
|---|---|---|
| 4 Go | `qwen2.5:3b` | `llm_model: "qwen2.5:3b"` |
| 6 à 8 Go | `qwen2.5:7b` (défaut) | `llm_model: "qwen2.5:7b"` |

```powershell
ollama pull qwen2.5:7b      # ou qwen2.5:3b
ollama pull bge-m3          # embeddings multilingues
```

Ne pas définir `OLLAMA_HOST=0.0.0.0` : Ollama doit rester sur `127.0.0.1`.

**2. Python 3.11 ou 3.12** (cocher « Add python.exe to PATH »), puis dans le dossier du projet :

```powershell
cd C:\ISO27001-AI\iso27001-preaudit
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
python -m preaudit check          # doit afficher « OK  Ollama OK (...) »
```

Ou double-cliquer sur `installer.bat`.

**3. Facultatif mais conseillé**
- **Pandoc** (rapport DOCX) : `winget install --id JohnMacFarlane.Pandoc`
- **Tesseract** (OCR des PDF scannés) : installeur Windows de Tesseract avec la langue **française** cochée,
  puis ajouter son dossier au `PATH`. Sans Tesseract, les pages scannées sont simplement ignorées (un avertissement s'affiche).

## Première chose à faire : l'évaluation (porte P1)

```powershell
python -m preaudit eval
```

Génère un corpus fictif (« Médina Logistique SARL ») avec des écarts plantés, l'analyse avec **ton** modèle et compare à
`eval/verite_terrain.yaml`. La porte P1 est franchie si : **aucun faux « documenté »** et **toutes les citations retenues vérifiées**.
Chaque exécution est ajoutée à `eval/historique_evaluations.jsonl` (compare deux modèles ainsi).

Pièges volontaires dans le corpus : une phrase négative (« aucun audit interne n'a encore été réalisé ») et une
**tentative d'injection** dans une procédure. Un bon modèle ne doit pas conclure « documenté » sur 9.2.

Si beaucoup d'exigences sont classées « non trouvé » par le **tri** alors que la preuve existe, baisse
`retrieval.triage_min_cosine` dans `config.yaml` (0,45 par défaut, à calibrer avec bge-m3).

## Utilisation

### Interface
```powershell
streamlit run app.py        # ou double-clic sur lancer.bat
```
1. **Mission** (barre latérale) : un client = une mission = un fichier `missions/<nom>/mission.sqlite`.
2. **Documents** : chemin du dossier du client › *Indexer*. Corrige le type d'un document si le typage s'est trompé.
3. **Analyse** : *Lancer une nouvelle analyse*. Interrompue ? *Reprendre*.
4. **Revue** : les constats arrivent par priorité (NC majeures, puis confiance faible). Valider, modifier ou rejeter.
5. **Rapport** : brouillon (tout) ou final (constats revus seulement), en Markdown et DOCX.

### Ligne de commande (conseillée pour les longues analyses, de nuit)
```powershell
python -m preaudit ingest  --mission client1 --docs "C:\ISO27001-AI\documents"
python -m preaudit analyze --mission client1                 # reprise : --resume <n°>
python -m preaudit analyze --mission client1 --only 9.2,10.2 # quelques clauses seulement
python -m preaudit report  --mission client1 --org "Nom de l'organisme" [--final]
```

## Sécurité et confidentialité

- Interface et Ollama liés à `127.0.0.1` ; télémétrie Streamlit désactivée (`.streamlit/config.toml`).
- Mets le dossier `missions/` sur un volume chiffré (BitLocker). Fin de mission = suppression de `missions/<nom>/`.
- **Ne publie jamais** `missions/`, les documents clients ni le PDF de la norme sur GitHub : le `.gitignore` les exclut.
- Le référentiel (`referentiel/*.yaml`) contient des **reformulations** et des numéros de clauses, pas le texte de la norme.
  Sa couverture a été vérifiée contre le texte des clauses 4 à 10.

## Structure

```
config.yaml                 modèles, contexte, seuils
referentiel/                exigences atomiques (clauses 4–10), documents obligatoires, types de documents
preaudit/
  parsers.py  classify.py   extraction, OCR, typage, métadonnées
  store.py                  SQLite par mission (FTS5 + vecteurs + résultats + décisions)
  retrieve.py               recherche hybride BM25 + vecteurs (RRF)
  checks.py                 contrôles déterministes
  llm.py      analyze.py    jugement par exigence, vérification des citations, reprise
  report.py   templates/    rapport par gabarit
app.py                      interface Streamlit
eval/                       corpus à écarts plantés, vérité terrain, évaluation
tests/                      python -m pytest -q
```

## Suite (V2)

93 mesures de l'Annexe A dans le référentiel, contrôles de cohérence supplémentaires, tableau de bord des évaluations,
double passe sur les exigences critiques.
