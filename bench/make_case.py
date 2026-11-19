"""Génère un cas de test (diff + étiquettes) à partir d'une cible de l'environnement.

Pour la cible ``<nom>`` (déjà préparée par ``setup_env.py``) :

1. crée la branche ``degrade`` depuis le tag ``sain`` ;
2. supprime la documentation listée dans ``bench/bugs/<nom>.json`` (commit « doc ») ;
3. applique les mutations déterministes (commit « bugs ») ;
4. écrit ``environment/cases/<nom>.diff`` (diff du commit « bugs », donc le code
   seul) et ``environment/cases/<nom>.labels.json``, au format attendu par
   ``python -m prreview quality --labels``.

Les étiquettes viennent des mutations réellement appliquées : aucune n'est
ajustée à la main après coup. Usage : ``python bench/make_case.py [nom ...]``
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

RACINE = Path(__file__).resolve().parent.parent
CIBLES = RACINE / "environment" / "targets"
CAS = RACINE / "environment" / "cases"
BUGS = RACINE / "bench" / "bugs"
IDENTITE = ["-c", "user.name=bench", "-c", "user.email=bench@example.invalid"]


def git(dossier: Path, *args: str) -> str:
    """Exécute une commande git dans ``dossier`` et renvoie sa sortie."""
    res = subprocess.run(
        ["git", *IDENTITE, *args],
        cwd=dossier,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return res.stdout


def appliquer(dossier: Path, mutation: dict[str, str]) -> dict[str, Any]:
    """Applique une mutation (texte exact, présent une seule fois) et renvoie l'étiquette."""
    chemin = dossier / mutation["file"]
    with open(chemin, encoding="utf-8", newline="") as f:
        texte = f.read()
    # Les fins de ligne du dépôt (LF ou CRLF) sont conservées telles quelles.
    eol = "\r\n" if "\r\n" in texte else "\n"
    cherche = mutation["find"].replace("\n", eol)
    remplace = mutation["replace"].replace("\n", eol)
    if texte.count(cherche) != 1:
        raise ValueError(
            f"{mutation['id']} : motif présent {texte.count(cherche)} fois dans {mutation['file']}"
        )
    debut = texte.index(cherche)
    ligne = texte.count(eol, 0, debut) + 1
    with open(chemin, "w", encoding="utf-8", newline="") as f:
        f.write(texte.replace(cherche, remplace))
    return {
        "path": mutation["file"],
        "line": ligne,
        "category": mutation["category"],
        "note": mutation["note"],
    }


def generer(nom: str) -> tuple[Path, Path]:
    """Génère le diff et les étiquettes d'une cible ; renvoie leurs chemins."""
    dossier = CIBLES / nom
    if not dossier.exists():
        raise SystemExit(f"Cible absente : lancer d'abord bench/setup_env.py ({nom})")
    spec = json.loads((BUGS / f"{nom}.json").read_text(encoding="utf-8"))

    git(dossier, "checkout", "-q", "-f", "-B", "degrade", "refs/tags/sain")
    for rel in spec["strip"]:
        if (dossier / rel).exists():
            git(dossier, "rm", "-q", "-r", rel)
    git(dossier, "commit", "-q", "--allow-empty", "-m", "doc supprimée")
    base = git(dossier, "rev-parse", "HEAD").strip()

    etiquettes = [appliquer(dossier, m) for m in spec["mutations"]]
    git(dossier, "commit", "-q", "-a", "-m", "bugs injectés")
    diff = git(dossier, "diff", "--no-color", "-U3", base, "HEAD")

    CAS.mkdir(parents=True, exist_ok=True)
    chemin_diff = CAS / f"{nom}.diff"
    chemin_diff.write_text(diff, encoding="utf-8", newline="\n")
    cibles = json.loads((RACINE / "bench" / "targets.json").read_text(encoding="utf-8"))
    cible = next(c for c in cibles if c["name"] == nom)
    chemin_labels = CAS / f"{nom}.labels.json"
    contenu = {
        "target": f"{nom} ({cible['language']}, {cible['license']}) - bugs injectés",
        "diff": f"{nom}.diff",
        "annotator": "bench/make_case.py (mutations déterministes)",
        "protocol": (
            "Chaque étiquette correspond à une mutation appliquée ; ligne = première "
            "ligne modifiée dans le fichier dégradé ; tolérance +/-3 lignes."
        ),
        "base": base,
        "labels": etiquettes,
    }
    chemin_labels.write_text(
        json.dumps(contenu, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return chemin_diff, chemin_labels


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("noms", nargs="*", help="cibles (défaut : toutes)")
    args = parser.parse_args()
    noms = args.noms or sorted(p.stem for p in BUGS.glob("*.json"))
    for nom in noms:
        d, lab = generer(nom)
        print(f"{nom}: {d.relative_to(RACINE)} + {lab.relative_to(RACINE)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
