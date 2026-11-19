"""Prépare l'environnement de test local : clone les cibles à un commit figé.

Les cibles sont placées dans ``environment/targets/<nom>`` (ignoré par Git).
Chaque cible reçoit un tag ``sain`` sur le commit de référence, qui permet de
revenir à l'état d'origine après l'injection de bugs.

Usage : ``python bench/setup_env.py [--reset]``
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

RACINE = Path(__file__).resolve().parent.parent
CIBLES_JSON = RACINE / "bench" / "targets.json"
DOSSIER_ENV = RACINE / "environment" / "targets"
LICENCES_AUTORISEES = {"MIT", "Apache-2.0", "BSD-3-Clause", "BSD-2-Clause"}


def git(dossier: Path, *args: str) -> str:
    """Exécute une commande git dans ``dossier`` et renvoie sa sortie."""
    res = subprocess.run(
        ["git", *args], cwd=dossier, capture_output=True, text=True, check=True
    )
    return res.stdout.strip()


def charger_cibles() -> list[dict[str, object]]:
    """Charge la liste des cibles et refuse toute licence non vérifiée."""
    cibles: list[dict[str, object]] = json.loads(CIBLES_JSON.read_text(encoding="utf-8"))
    for c in cibles:
        if c["license"] not in LICENCES_AUTORISEES:
            raise ValueError(f"Licence non autorisée pour {c['name']} : {c['license']}")
    return cibles


def preparer(cible: dict[str, object], reset: bool) -> str:
    """Clone une cible au commit figé et pose le tag ``sain``."""
    nom = str(cible["name"])
    sha = str(cible["sha"])
    dossier = DOSSIER_ENV / nom
    if dossier.exists() and not reset:
        if git(dossier, "rev-parse", "refs/tags/sain") == sha:
            return "déjà prête"
        raise RuntimeError(f"{nom} existe mais diffère du commit figé (utiliser --reset)")
    if dossier.exists():
        # On ne supprime que sous environment/targets/ (garde-fou).
        if DOSSIER_ENV not in dossier.resolve().parents:
            raise RuntimeError(f"Chemin inattendu : {dossier}")
        import shutil

        shutil.rmtree(dossier, onerror=_forcer_suppression)
    dossier.mkdir(parents=True)
    git(dossier, "init", "-q")
    git(dossier, "remote", "add", "origin", f"https://github.com/{cible['repo']}.git")
    git(dossier, "fetch", "-q", "--depth", "1", "origin", sha)
    git(dossier, "checkout", "-q", "-b", "travail", sha)
    git(dossier, "tag", "sain", sha)
    return "clonée"


def _forcer_suppression(func: object, chemin: str, _exc: object) -> None:
    """Rend un fichier en lecture seule supprimable (cas fréquent de ``.git`` sous Windows)."""
    import os
    import stat

    os.chmod(chemin, stat.S_IWRITE)
    func(chemin)  # type: ignore[operator]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reset", action="store_true", help="re-cloner les cibles")
    args = parser.parse_args()
    for cible in charger_cibles():
        etat = preparer(cible, args.reset)
        print(f"{cible['name']:10} ({cible['language']}, {cible['license']}) : {etat}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
