"""Vocabulaire des constats (inspiré d'ISO 19011 / 17021) et règles de classement."""

NC_MAJ = "NC majeure potentielle"
NC_MIN = "NC mineure potentielle"
OBS = "Observation"
OFI = "Opportunité d'amélioration"
DOC = "Documenté"
REVIEW = "À revoir"          # le modèle a conclu favorablement sans citation vérifiable
OK = "Conforme (contrôle automatique)"

ORDER = [NC_MAJ, NC_MIN, REVIEW, OBS, OFI, DOC, OK]
RANK = {c: i for i, c in enumerate(ORDER)}

STATUS_LABELS = {
    "documente": "Documenté",
    "partiel": "Partiellement documenté",
    "non_trouve": "Non trouvé dans les documents fournis",
    "a_revoir": "À revoir (citation non vérifiée)",
    "erreur": "Erreur d'analyse",
}


def classify_requirement(status: str, critique: bool) -> str:
    if status == "non_trouve":
        return NC_MAJ if critique else NC_MIN
    if status == "partiel":
        return NC_MIN if critique else OBS
    if status == "a_revoir" or status == "erreur":
        return REVIEW
    return DOC


def worst(classifications: list[str]) -> str:
    return min(classifications, key=lambda c: RANK.get(c, 99)) if classifications else DOC
