# Agent de code review agentique

**Projet final — Advanced Programming and Parallel Computing, MIASHS 2026–2027**
Sujet : *Agentic code review agent* (§14.2.1)

Dépôt : `pr-review-agent/` · Code : `src/prreview/` · Mesures reproductibles : `make all`

---

## 1. Objectifs

Le sujet demande un assistant **agentique** qui relit un diff de pull request et
produit des findings structurés (bug, style, tests manquants) avec sévérité et
justification. Il désigne explicitement deux difficultés : **la boucle d'agent**
et **la parallélisation** — relire plusieurs fichiers en parallèle, puis un agent
« juge » qui déduplique et filtre les findings peu fiables avant publication.

Nous nous sommes fixé trois objectifs vérifiables, chacun associé à une mesure
plutôt qu'à une affirmation :

1. **Agentique, pas pipeline.** Le modèle choisit lui-même quels outils appeler,
   dans quel ordre, et quand il en sait assez. Vérifié par la trace : chaque
   revue de fichier enregistre ses étapes et ses appels d'outils.
2. **Parallélisme réel.** Pas de séquentiel déguisé. Vérifié par trois contrôles
   indépendants (§5.1), dont un chien de garde qui mesure le blocage de la
   boucle d'événements.
3. **Revue réellement utile.** Vérifié par précision / rappel / F1 contre des
   défauts labellisés à la main, avec un balayage de seuil (§5.3).

Livrables produits : un **CLI** et un **serveur MCP** (le sujet en demandait un
des deux), un benchmark de scalabilité, un benchmark qualité sur PR labellisée,
et une comparaison avec une baseline non-LLM.

---

## 2. Architecture

```
                      ┌──────────────────────────────────────────┐
  cible               │  GitHub REST  /pulls/{n}/files           │
  ────────────────────┤  ou  fichier .diff local (mode hors-ligne)│
  PR URL | owner/repo#N│                                          │
  | chemin .diff       └────────────────────┬─────────────────────┘
                                            │  List[FileDiff] (hunks + n° de ligne absolus)
                                            ▼
                      ┌─────────────────────────────────────────────────┐
                      │  Orchestrateur  (asyncio)                       │
                      │  tâches = fichiers × rôles                      │
                      │  Semaphore(N)  ← point de backpressure          │
                      │  tri par coût décroissant (LPT)                 │
                      └───┬───────────┬───────────┬─────────────────────┘
                          │           │           │   … N agents en vol
              ┌───────────▼─┐ ┌───────▼─────┐ ┌───▼─────────┐
              │ correctness │ │ performance │ │   tests     │   ← boucle d'agent
              │  tool loop  │ │  tool loop  │ │  tool loop  │
              └───────┬─────┘ └──────┬──────┘ └──────┬──────┘
                      │              │               │
                      │   outils (lecture seule, sandboxés) :
                      │   get_diff_hunks · get_file_context · grep_repo
                      │   find_tests_for · list_changed_files
                      │              │               │
                      └──────────────┼───────────────┘
                                     ▼   findings typés et validés
                      ┌─────────────────────────────────────────────────┐
                      │  Agent juge                                     │
                      │  1. dédoublonnage (fichier+catégorie+titre/ligne)│
                      │  2. corroboration  (accord inter-rôles → conf.) │
                      │  3. filtrage       (seuil de confiance)         │
                      │  4. plafonnement   (max N findings)             │
                      │  (+ reformulation LLM optionnelle, bornée)      │
                      └────────────────────┬────────────────────────────┘
                                           ▼
                 terminal · markdown · JSON · payload de review GitHub
```

**L'unité de travail est le couple (fichier, rôle), pas le fichier.** Avec 4
fichiers et 3 rôles cela fait 12 boucles d'agent indépendantes. C'est un choix
structurant : une parallélisation par fichier seul ne produirait que 4 tâches et
ne saturerait jamais un sémaphore de 8. Cela donne en plus la redondance que le
juge exploite — deux rôles qui trouvent indépendamment le même défaut sont une
preuve plus forte qu'un seul.

### 2.1 La boucle d'agent (`reviewer.py`)

À chaque étape, le modèle peut soit appeler un outil, soit répondre. La boucle
n'impose que les invariants qu'on ne peut pas lui confier :

- un **budget d'étapes** (6) et un **budget d'appels d'outils** (8) ;
- la **validation JSON** avec récupération : fence de code, prose autour du JSON,
  liste nue, objet unique. Si rien n'est exploitable, on redemande explicitement
  du JSON au lieu d'abandonner le fichier ;
- une **auto-vérification** : un finding qui cite une ligne que le diff ne touche
  pas est **supprimé** ; à ±3 lignes près il est recalé sur la ligne réelle.
  C'est la plus grosse source de faux positifs d'une implémentation naïve, parce
  qu'un modèle ne voit que des marqueurs `+`/`-` et invente des numéros. Nous
  annotons donc le diff avec les numéros de ligne réels du nouveau fichier avant
  de l'envoyer (`diffparse.numbered_patch`).

Les erreurs d'outils sont **renvoyées au modèle comme texte** (`ERROR: ...` avec
le schéma attendu) au lieu d'être levées : l'agent se corrige à l'étape suivante.
Lever l'exception ferait échouer toute la revue du fichier pour un argument
halluciné.

### 2.2 L'agent juge (`judge.py`)

Les étapes 1 à 4 sont **déterministes**, donc testables unitairement. La
corroboration ajoute un bonus borné par rôle d'accord, plafonné à 0,99 : l'accord
ne peut jamais fabriquer de certitude. Un défaut de sévérité `high` trouvé
indépendamment par deux rôles survit même sous le seuil — il mérite un coup d'œil
humain.

La reformulation par LLM est optionnelle et **ne peut ni ajouter, ni supprimer,
ni réordonner** : les réécritures sont rapprochées des empreintes des findings
existants et tout le reste est jeté.

---

## 3. Choix techniques

**asyncio plutôt que multiprocessing.** La charge est dominée par l'attente
réseau (appels HTTP au modèle), pas par le calcul. Un pool de processus paierait
la sérialisation des données pour un travail CPU quasi nul. Le parsing de diff,
seule partie un peu calculatoire, est fait **avant** le démarrage de la boucle.

**Sémaphore, pas `gather` nu.** Un `asyncio.gather` sur une PR de 200 fichiers
ouvre 200 requêtes simultanées : le serveur répond 429 et la tempête de retries
rend l'exécution *plus lente* que le séquentiel. Le sémaphore **est** le
mécanisme de backpressure.

**`return_exceptions=True`.** Sans cela, un agent qui lève annule les onze
autres. Les échecs sont enregistrés par tâche et remontés dans le rapport
(`failed_reviewers`), ce qui rend la dégradation visible au lieu de silencieuse.

**Ordonnancement plus-long-d'abord.** Les tâches sont soumises par coût
décroissant (`FileDiff.cost_hint`). Avec un pool borné, finir les longues tâches
en dernier laisse le pool inoccupé en fin d'exécution ; les démarrer d'abord
raccourcit la traîne (*longest-processing-time-first*).

**Round-robin sur plusieurs endpoints.** Avec un seul serveur, augmenter la
concurrence au-delà de sa taille de batch ne fait qu'allonger une file.
`MultiEndpointClient` répartit sur les 7 endpoints fournis : c'est ce qui
convertit la concurrence en gain de temps réel.

**Backoff exponentiel avec gigue.** La gigue est nécessaire, pas cosmétique :
N reviewers démarrés ensemble réessaieraient en phase et recréeraient
exactement la rafale qui a provoqué le 429.

**Zéro dépendance.** Tout tient dans la bibliothèque standard, y compris les
graphiques (SVG écrit à la main dans `render.py`). Le dépôt s'installe sans
réseau et les mesures sont reproductibles partout — un point que le cahier des
charges demande explicitement. La contrainte réelle : il n'existe pas de client
HTTP asynchrone dans la stdlib, donc l'appel bloquant `urllib` est poussé hors
de la boucle par `asyncio.to_thread`. CPython libère le GIL autour des appels
système socket, donc ces threads se recouvrent réellement dans le temps — c'est
de la concurrence d'E/S authentique, et §5.1 le mesure.

**Sandboxing des outils.** Tous les outils sont en lecture seule et confinés à la
racine du dépôt ; la traversée de chemin est refusée (test dédié). Un agent de
revue capable d'écrire ou d'exécuter serait une faille d'exécution de code à
distance, puisque le diff qu'il lit est une entrée non fiable.

---

## 4. Transport hors-ligne déterministe

`MockLLM` imite un modèle à appel d'outils : premier tour il demande un outil
(la boucle est donc réellement exercée), second tour il émet du JSON. Ses
findings dérivent du moteur de règles `rules.py`, filtrés par rôle : un reviewer
`performance` signale surtout du `perf`, et ne s'écarte de sa spécialité que
~30 % du temps, de façon déterministe (hash de rôle+fichier+ligne).

Ce filtrage par rôle n'est pas un détail. Dans une première version tous les
rôles signalaient tout : l'accord était unanime **par construction**, la
confiance saturait à 0,99 partout, et la logique de corroboration du juge
paraissait efficace alors qu'elle ne mesurait rien. Rendre les rôles réellement
divergents a fait passer les findings bruts de 66 à 32 et a étalé les confiances.

La latence simulée est un `await asyncio.sleep` — une vraie attente — donc la
concurrence est mesurée honnêtement. Mais **le speedup obtenu décrit
l'orchestrateur, pas le débit d'un vrai modèle** : nous l'énonçons explicitement
partout où le chiffre apparaît.

---

## 5. Mesures

### 5.1 Le parallélisme est-il réel ? (`scripts/prove_concurrency.py`)

« C'est plus rapide » ne prouve rien. Trois contrôles indépendants :

| Configuration | Temps mur | Somme temps agents | Rapport | Recouvrement max | Pire blocage boucle |
|---|---:|---:|---:|---:|---:|
| séquentiel | 3,73 s | 3,73 s | 1,0× | 1 | 1,8 ms |
| parallèle c=4 | 0,96 s | 3,73 s | 3,9× | 4 | 2,0 ms |
| parallèle c=12 | 0,35 s | 3,72 s | 10,7× | 12 | 1,4 ms |

1. **Recouvrement des fenêtres d'exécution** (balayage sur les événements
   début/fin) : 12 tâches simultanées en pointe. Une implémentation séquentielle
   rapporterait 1 quel que soit son temps mur.
2. **Somme des temps agents ≫ temps mur** : 3,72 s de travail en 0,35 s.
3. **Chien de garde de la boucle d'événements** : une coroutine tique toutes les
   10 ms et mesure son pire écart. Jamais au-dessus de 2 ms. Un appel bloquant
   dans la boucle (`time.sleep`, HTTP synchrone) apparaîtrait ici comme un
   décrochage long — c'est précisément le « faux parallélisme » que le barème
   sanctionne, et voici comment le détecter.

Les trois configurations publient **les mêmes 18 findings** : la concurrence
change le temps, pas le résultat.

### 5.2 Scalabilité (`make speedup`, médiane de 3 exécutions, latence 200 ms)

| Mode | Concurrence | Temps mur | Speedup | Efficacité | Recouvrement |
|---|---:|---:|---:|---:|---:|
| séquentiel | 1 | 5,22 s | 1,00× | 100 % | 1 |
| parallèle | 2 | 2,81 s | 1,86× | 93 % | 2 |
| parallèle | 4 | 1,46 s | 3,59× | 90 % | 4 |
| parallèle | 8 | 0,88 s | 5,95× | 74 % | 8 |
| parallèle | 12 | 0,58 s | 9,02× | 75 % | 12 |

Graphiques : `results/speedup.svg` (speedup vs idéal linéaire) et
`results/timeline.svg` (diagramme de Gantt des 12 tâches).

**Interprétation.** L'efficacité reste ≥ 90 % jusqu'à 4, puis tombe à ~75 %. Ce
n'est pas une contention de verrou : chaque agent effectue **deux appels modèle
séquentiels** (une étape d'outil, une étape de réponse). Le chemin critique vaut
donc 2 × latence ≈ 0,4 s, et 12 agents ne peuvent pas atteindre 12× — la borne
est 5,22 / 0,4 ≈ 13× seulement si tout se recouvre parfaitement. L'écart restant
est l'ordonnancement de la boucle et le coût de création des tâches. Autrement
dit : la limite est **la profondeur de la boucle d'agent**, pas la largeur du
sémaphore. Réduire le nombre d'étapes (cache d'outils, outils groupés) gagnerait
plus que monter la concurrence.

L'écart-type vaut 0,00 par construction : la séquence de délais du mock est
semée, donc les répétitions sont identiques au bit près. Il ne devient
informatif que sur de vrais endpoints.

### 5.3 Qualité de la revue (`make quality`, 16 défauts labellisés)

Protocole : deux relecteurs ont labellisé indépendamment puis réconcilié ; un
défaut n'est retenu que si un mainteneur demanderait plausiblement une
modification. Tolérance d'appariement ±3 lignes, appariement **un-à-un** (sans
quoi trois findings sur la même ligne compteraient trois vrais positifs et
gonfleraient la précision).

| Configuration | Seuil | Publiés | VP | FP | FN | Précision | Rappel | F1 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| baseline à règles | — | 22 | 14 | 8 | 2 | 0,64 | 0,88 | **0,74** |
| agents | 0,00 | 18 | 11 | 7 | 5 | 0,61 | 0,69 | **0,65** |
| agents | 0,60 | 13 | 10 | 3 | 6 | 0,77 | 0,62 | **0,69** |
| agents | 0,75 | 11 | 9 | 2 | 7 | 0,82 | 0,56 | **0,67** |
| agents | 0,90 | 5 | 5 | 0 | 11 | 1,00 | 0,31 | **0,48** |

Le balayage de seuil donne exactement le compromis attendu : la précision monte
de 0,61 à 1,00 pendant que le rappel s'effondre de 0,69 à 0,31, avec un F1
maximal à 0,69 pour un seuil de 0,60. **C'est l'expérience qui justifie le
filtrage du juge**, au lieu de le postuler.

**Lecture honnête du tableau.** La baseline à règles *bat* le pipeline agentique,
et c'est inévitable : le mock hors-ligne dérive ses findings du même moteur de
règles, il ne peut donc rien savoir que les règles ignorent, tandis que le
filtrage par rôle et le dédoublonnage ne peuvent que retirer de l'information. Ce
benchmark valide donc **le harnais de mesure** — appariement, score, balayage —
et non la qualité d'un modèle de langage. Les deux défauts **sémantiques** semés
volontairement dans la vérité terrain (`fetcher.py:36`, un `IndexError` sur liste
vide ; `cache.py:27`, une éviction qui retire la clé la plus petite au lieu de la
plus ancienne) sont hors de portée de toute règle syntaxique : ce sont eux qui
mesureraient un vrai modèle. La même commande avec `PRREVIEW_ENDPOINTS` renseigné
produit cette mesure.

---

## 6. Limites

- **Fusion trop agressive du juge.** Deux défauts distincts de même catégorie sur
  des lignes adjacentes sont rapportés comme un seul. Mesuré : avec
  `line_window=3`, l'appel bloquant de `fetcher.py:15` est absorbé par celui de
  `:14` ; avec `line_window=0`, les deux apparaissent. Le F1 ne bouge pas (le
  problème est la granularité du rapport, pas le score), mais un relecteur
  humain perd une information. Le paramètre existe dans `JudgeConfig` et n'est
  pas encore exposé en CLI.
- **Détection du contexte `async` approximative.** `rules.py` déduit
  l'appartenance à une coroutine par indentation **à l'intérieur du diff** : un
  hunk qui commence au milieu d'une fonction ne voit pas sa signature englobante,
  donc un appel bloquant y passe inaperçu. Documenté dans le code plutôt que
  masqué.
- **Dépendant de l'appel d'outils.** Seul `/v1/chat/completions` avec `tools` est
  implémenté. Un modèle sans appel d'outils retomberait sur une unique étape de
  réponse et cesserait d'être agentique.
- **Diffs volumineux et binaires.** L'API GitHub omet `patch` au-delà d'une
  certaine taille ; ces fichiers sont ignorés plutôt que récupérés séparément.
- **Vérité terrain restreinte.** 16 défauts sur une PR synthétique. Le sujet
  suggérait 10–20 PR réelles labellisées ; c'est l'extension la plus utile, et le
  format de `bench/*.labels.json` est déjà prévu pour plusieurs cas.
- **Pas de publication réelle.** `--format github` produit le payload de review
  mais ne l'envoie pas : poster des commentaires sur la PR d'un tiers est un
  effet de bord qui appartient à l'utilisateur, pas à l'outil.

---

## 7. Ce que nous avons appris

**Le parallélisme ne se raconte pas, il se mesure — et il faut mesurer la bonne
chose.** Le temps mur seul ne distingue pas la concurrence d'un modèle plus
rapide. Les trois contrôles de §5.1 (recouvrement, somme/mur, blocage de boucle)
se sont révélés être le vrai livrable technique : le chien de garde de boucle
d'événements est un outil que nous réutiliserons, parce qu'il transforme
« asyncio est-il vraiment concurrent ici ? » en un nombre.

**La borne de scalabilité était là où nous ne l'attendions pas.** Nous avons
instinctivement cherché à élargir le sémaphore ; la mesure montre que le facteur
limitant est la *profondeur* de la boucle d'agent (deux appels séquentiels par
tâche). Sans le tableau d'efficacité, nous aurions optimisé le mauvais axe.

**Un benchmark peut être rigoureux et donner un résultat défavorable.** La
baseline à règles bat nos agents, et l'expliquer correctement (le mock ne peut
pas dépasser les règles dont il dérive) valait mieux que de bricoler le protocole
jusqu'à obtenir le chiffre souhaité. C'est aussi ce qui définit l'expérience
suivante : seuls les défauts sémantiques séparent un vrai modèle d'un linter.

**Les tests ont trouvé de vrais bugs, pas seulement des régressions.** Deux
défauts réels sont apparus pendant l'écriture de la suite : `dedupe_key`
supprimait tout jeton de ≤3 caractères et fusionnait donc silencieusement des
findings distincts (« missing test for `foo` » / « … `bar` ») ; et le balayage de
recouvrement comptait une tâche finissant en *t* et une démarrant en *t* comme
simultanées, si bien qu'une exécution strictement séquentielle rapportait une
concurrence de 2 — exactement la métrique censée détecter le faux parallélisme.
Les deux sont corrigés et couverts.

**Le mode hors-ligne déterministe a été la meilleure décision d'ingénierie.**
Il rend le dépôt installable sans réseau, les tests exécutables partout et les
mesures reproductibles ; il a aussi forcé une question de conception utile — que
signifie « des rôles qui divergent » — dont la réponse a changé le comportement
du juge.

---

## Annexe — reproduire les mesures

```bash
export PYTHONPATH=src
make test       # 49 tests, bibliothèque standard uniquement
make proof      # les trois contrôles de parallélisme (§5.1)
make speedup    # tableau de scalabilité + results/speedup.svg + timeline.svg
make quality    # précision / rappel / F1 + results/quality.svg
```

Artefacts générés : `results/speedup.{json,md,svg}`, `results/quality.{json,md,svg}`,
`results/timeline.svg`, `results/demo_run.json`.

**Répartition du travail** *(à compléter par le binôme : qui a écrit la boucle
d'agent, qui l'orchestrateur et le juge, qui le protocole de labellisation et les
benchmarks — le cahier des charges demande que les rôles soient explicités.)*
