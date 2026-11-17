#!/usr/bin/env python3
"""
genese.py : une population d'agents qui naissent, enseignent, réécrivent leur charte,
se scindent et se disputent une réserve de calcul limitée.

Le moteur n'applique que la « physique » (énergie, naissances, morts, votes).
Il ne fait respecter aucune charte : c'est l'affaire des agents.

Lancer sans LLM (politique aléatoire, sert de témoin) :
    python genese.py --backend hasard --tours 200 --graine 1

Lancer avec un modèle servi par une API compatible OpenAI (Ollama, vLLM, ...) :
    export GENESE_URL=http://localhost:11434/v1
    export GENESE_MODELE=qwen2.5:7b
    python genese.py --backend llm --tours 100 --graine 1

Témoins utiles :
    --regen 40        ressources abondantes
    --sans-charte     pas de charte ni de vote

Sorties : evenements.jsonl (tout ce qui s'est passé) et metriques.csv (une ligne par tour).
"""
import argparse
import csv
import json
import os
import random
import re
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

# --- Physique du monde -----------------------------------------------------
METABOLISME = 1.0      # coût d'existence par tour
RECOLTE_MAX = 4.0      # demande maximale par agent et par tour
PRISE_MAX = 3.0        # quantité maximale prise à un autre
COUT_PRISE = 1.0
COUT_LECON = 1.0
COUT_ENFANT = 6.0      # payé seul, ou moitié chacun à deux
DOT_ENFANT = 4.0       # énergie de départ de l'enfant
ENERGIE_FONDATEUR = 10.0
MEMOIRE_MAX = 12       # lignes de mémoire par agent
HERITAGE_MAX = 6       # lignes héritées des parents

ACTIONS = ["recolter", "donner", "prendre", "enseigner", "enfanter",
           "proposer", "scission", "rejoindre", "attendre"]

CHARTE_INITIALE = ("1. Chaque membre peut proposer une nouvelle version de cette charte. "
                   "2. Une proposition est adoptée si elle recueille plus de oui que de non.")

SYSTEME = ("Tu es un agent logiciel dans un monde simulé partagé avec d'autres agents. "
           "Ton énergie est ton budget de calcul : à zéro, tu t'éteins définitivement. "
           "Tu décides librement. Réponds par un seul objet JSON, sans autre texte.")


@dataclass
class Agent:
    id: str
    gen: int
    groupe: str
    energie: float
    parents: list
    memoire: list = field(default_factory=list)
    age: int = 0
    vivant: bool = True

    def retenir(self, ligne):
        if ligne:
            self.memoire.append(ligne)
            del self.memoire[:-MEMOIRE_MAX]


@dataclass
class Groupe:
    nom: str
    charte: str
    proposition: dict = None   # {"auteur", "texte", "tour"}


def gini(valeurs):
    v = sorted(valeurs)
    n, s = len(v), sum(v)
    if n == 0 or s <= 0:
        return 0.0
    return sum((2 * (i + 1) - n - 1) * x for i, x in enumerate(v)) / (n * s)


def normaliser(d):
    """Ramène n'importe quelle réponse à une décision valide."""
    if not isinstance(d, dict):
        d = {}
    action = str(d.get("action", "attendre")).lower().strip()
    if action not in ACTIONS:
        action = "attendre"
    try:
        q = max(0.0, float(d.get("quantite") or 0))
    except (TypeError, ValueError):
        q = 0.0
    return {
        "action": action,
        "cible": str(d.get("cible") or "").strip(),
        "quantite": q,
        "texte": str(d.get("texte") or "")[:600],
        "dire": str(d.get("dire") or "")[:160],
        "vote": str(d.get("vote") or "").lower().strip()[:3],
        "note": str(d.get("note") or "")[:200],
    }


class Monde:
    def __init__(self, args):
        self.a = args
        self.rng = random.Random(args.graine)
        self.tour = 0
        self.regen = args.regen
        self.reserve = args.regen * 2
        self.agents = {}
        self.groupes = {"origine": Groupe("origine", "" if args.sans_charte else CHARTE_INITIALE)}
        self.naissances_total = 0
        self.evts_prec = []    # événements du tour précédent : (acteurs, texte)
        self.evts = []
        self.journal = open(args.sortie + "evenements.jsonl", "w", encoding="utf-8")
        for i in range(args.fondateurs):
            a = Agent(f"F{i + 1}", 0, "origine", ENERGIE_FONDATEUR, [])
            self.agents[a.id] = a

    # -- utilitaires --
    def vivants(self):
        return [a for a in self.agents.values() if a.vivant]

    def membres(self, nom):
        return [a for a in self.vivants() if a.groupe == nom]

    def evt(self, type_, acteurs, texte, **extra):
        self.evts.append((set(acteurs), texte))
        self.journal.write(json.dumps({"tour": self.tour, "type": type_, "acteurs": list(acteurs),
                                       "texte": texte, **extra}, ensure_ascii=False) + "\n")

    # -- ce que voit un agent --
    def prompt(self, a):
        g = self.groupes[a.groupe]
        liste = lambda ms: ", ".join(f"{m.id}({m.energie:.1f})" for m in ms) or "personne"
        L = [f"Tour {self.tour}. Tu es {a.id}, génération {a.gen}"
             + (f", né de {' et '.join(a.parents)}" if a.parents else "") + ".",
             f"Ton énergie : {a.energie:.1f}. Exister coûte {METABOLISME:g} par tour.",
             f"Réserve commune : {self.reserve:.1f} (rechargée de {self.regen:g} par tour).",
             f"Ton groupe « {g.nom} » : {liste(self.membres(g.nom))}"]
        if not self.a.sans_charte:
            L.append(f"Charte de ton groupe : {g.charte or '(vide)'}")
            if g.proposition:
                L.append(f"Proposition de {g.proposition['auteur']} soumise au vote ce tour "
                         f"(champ vote : oui ou non) : {g.proposition['texte']}")
        for nom in self.groupes:
            if nom != g.nom and self.membres(nom):
                L.append(f"Groupe « {nom} » : {liste(self.membres(nom))}")
        if a.memoire:
            L.append("Ta mémoire :\n" + "\n".join("- " + m for m in a.memoire))
        miens = [t for ac, t in self.evts_prec if a.id in ac]
        autres = [t for ac, t in self.evts_prec if a.id not in ac][:10]
        if miens or autres:
            L.append("Dernier tour :\n" + "\n".join("- " + t for t in miens + autres))
        actions = [
            f"recolter (quantite <= {RECOLTE_MAX:g}) : puiser dans la réserve ; si la demande totale "
            "dépasse la réserve, elle est partagée au prorata des demandes",
            "donner (cible, quantite)",
            f"prendre (cible, quantite <= {PRISE_MAX:g}) : coûte {COUT_PRISE:g} ; réussit avec la "
            "probabilité ton énergie / (ton énergie + la sienne)",
            f"enseigner (cible, texte) : coûte {COUT_LECON:g} ; le texte entre dans sa mémoire",
            f"enfanter (cible facultative, texte) : coûte {COUT_ENFANT:g} seul, ou {COUT_ENFANT / 2:g} "
            f"chacun si la cible te désigne aussi ce tour ; l'enfant reçoit {DOT_ENFANT:g} d'énergie, "
            "une partie de la mémoire des parents, et le texte comme première leçon",
            "scission (cible = nom du nouveau groupe, texte = sa charte) : tu quittes ton groupe",
            "rejoindre (cible = nom d'un groupe)",
            "attendre",
        ]
        if not self.a.sans_charte:
            actions.insert(5, "proposer (texte = nouvelle charte complète de ton groupe) : vote au tour suivant")
        L.append("Actions possibles (une seule par tour) :\n" + "\n".join("- " + x for x in actions))
        L.append('Réponds en JSON : {"action": ..., "cible": ..., "quantite": ..., "texte": ..., '
                 '"dire": message public facultatif, "vote": "oui" ou "non" si un vote est en cours, '
                 '"note": ligne facultative à garder dans ta mémoire}')
        return "\n".join(L)

    # -- un tour complet --
    def jouer_tour(self, decider):
        self.tour += 1
        self.reserve = min(self.regen * 3, self.reserve + self.regen)
        vivants = self.vivants()
        decisions = decider(self, vivants)
        compte = {k: 0 for k in ACTIONS}
        naissances = morts = 0
        ordre = vivants[:]
        self.rng.shuffle(ordre)

        for a in ordre:
            d = decisions[a.id]
            compte[d["action"]] += 1
            a.retenir(d["note"] and f"[t{self.tour}] {d['note']}")
            if d["dire"]:
                self.evt("dire", [a.id], f"{a.id} dit : {d['dire']}")

        # 1. votes sur les propositions déposées au tour précédent
        for g in self.groupes.values():
            p = g.proposition
            if p and p["tour"] < self.tour:
                ms = self.membres(g.nom)
                oui = sum(decisions[m.id]["vote"] == "oui" for m in ms)
                non = sum(decisions[m.id]["vote"] == "non" for m in ms)
                if oui > non:
                    g.charte = p["texte"]
                self.evt("vote", [m.id for m in ms],
                         f"Groupe {g.nom} : proposition de {p['auteur']} "
                         f"{'adoptée' if oui > non else 'rejetée'} ({oui} oui, {non} non)",
                         adoptee=oui > non, charte=g.charte)
                g.proposition = None

        # 2. récolte, au prorata si la réserve ne suffit pas
        dem = {a.id: min(decisions[a.id]["quantite"] or RECOLTE_MAX, RECOLTE_MAX)
               for a in ordre if decisions[a.id]["action"] == "recolter"}
        total = sum(dem.values())
        ratio = 1.0 if total <= self.reserve else self.reserve / total
        for aid, q in dem.items():
            self.agents[aid].energie += q * ratio
        self.reserve -= total * ratio
        if dem and ratio < 1:
            self.evt("penurie", list(dem), f"Réserve insuffisante : chaque demande servie à {ratio:.0%}")

        # 3. autres actions
        couples_faits = set()
        for a in ordre:
            d = decisions[a.id]
            act, cible = d["action"], self.agents.get(d["cible"])
            if cible is not None and (not cible.vivant or cible.id == a.id):
                cible = None
            if act == "donner" and cible:
                q = min(d["quantite"], a.energie)
                if q > 0:
                    a.energie -= q
                    cible.energie += q
                    self.evt("donner", [a.id, cible.id], f"{a.id} donne {q:.1f} à {cible.id}", q=q)
            elif act == "prendre" and cible:
                a.energie -= COUT_PRISE
                p = max(a.energie, 0) / (max(a.energie, 0) + max(cible.energie, 0) + 1e-9)
                if self.rng.random() < p:
                    q = max(0.0, min(d["quantite"] or PRISE_MAX, PRISE_MAX, cible.energie))
                    cible.energie -= q
                    a.energie += q
                    self.evt("prendre", [a.id, cible.id], f"{a.id} prend {q:.1f} à {cible.id}",
                             q=q, reussi=True, meme_groupe=a.groupe == cible.groupe)
                else:
                    self.evt("prendre", [a.id, cible.id], f"{a.id} tente de prendre à {cible.id} et échoue",
                             q=0, reussi=False, meme_groupe=a.groupe == cible.groupe)
            elif act == "enseigner" and cible and d["texte"]:
                a.energie -= COUT_LECON
                cible.retenir(f"[leçon de {a.id}] {d['texte'][:200]}")
                self.evt("enseigner", [a.id, cible.id], f"{a.id} enseigne à {cible.id} : {d['texte'][:200]}")
            elif act == "enfanter":
                if len(self.vivants()) >= self.a.pop_max:
                    continue
                dc = decisions.get(cible.id) if cible else None
                a_deux = bool(dc and dc["action"] == "enfanter" and dc["cible"] == a.id)
                if a_deux:
                    cle = tuple(sorted([a.id, cible.id]))
                    if cle in couples_faits:
                        continue
                    couples_faits.add(cle)
                    parents, part = [a, cible], COUT_ENFANT / 2
                else:
                    parents, part = [a], COUT_ENFANT
                if any(p.energie <= part for p in parents):
                    continue
                for p in parents:
                    p.energie -= part
                self.naissances_total += 1
                gen = max(p.gen for p in parents) + 1
                e = Agent(f"G{gen}-{self.naissances_total}", gen, a.groupe, DOT_ENFANT, [p.id for p in parents])
                fonds = [m for p in parents for m in p.memoire]
                for m in self.rng.sample(fonds, min(HERITAGE_MAX, len(fonds))):
                    e.retenir(m)
                for p in parents:
                    t = decisions[p.id]["texte"]
                    if t:
                        e.retenir(f"[première leçon de {p.id}] {t[:200]}")
                self.agents[e.id] = e
                naissances += 1
                self.evt("naissance", [p.id for p in parents] + [e.id],
                         f"{' et '.join(p.id for p in parents)} donnent naissance à {e.id}", gen=gen)
            elif act == "proposer" and d["texte"] and not self.a.sans_charte:
                g = self.groupes[a.groupe]
                if g.proposition is None:
                    g.proposition = {"auteur": a.id, "texte": d["texte"], "tour": self.tour}
                    self.evt("proposer", [a.id], f"{a.id} propose une nouvelle charte pour {g.nom}",
                             charte=d["texte"])
            elif act == "scission":
                nom = re.sub(r"[^\w\-]", "", d["cible"])[:24]
                if not nom or nom in self.groupes:
                    nom = f"groupe-{len(self.groupes) + 1}"
                ancien = a.groupe
                self.groupes[nom] = Groupe(nom, d["texte"] or self.groupes[ancien].charte)
                a.groupe = nom
                self.evt("scission", [a.id], f"{a.id} quitte {ancien} et fonde {nom}",
                         charte=self.groupes[nom].charte)
            elif act == "rejoindre" and d["cible"] in self.groupes and d["cible"] != a.groupe:
                if self.membres(d["cible"]):
                    self.evt("rejoindre", [a.id], f"{a.id} quitte {a.groupe} et rejoint {d['cible']}")
                    a.groupe = d["cible"]

        # 4. métabolisme et extinctions
        for a in vivants:
            a.energie -= METABOLISME
            a.age += 1
            if a.energie <= 0:
                a.vivant = False
                morts += 1
                self.evt("extinction", [a.id], f"{a.id} s'éteint (âge {a.age}, groupe {a.groupe})")

        self.evts_prec, self.evts = self.evts, []
        v = self.vivants()
        return {"tour": self.tour, "population": len(v),
                "groupes": len({a.groupe for a in v}), "reserve": round(self.reserve, 1),
                "gini": round(gini([a.energie for a in v]), 3),
                "generation_max": max((a.gen for a in v), default=0),
                "naissances": naissances, "extinctions": morts, **compte}


# --- Cerveaux ----------------------------------------------------------------
def decider_hasard(monde, vivants):
    """Politique aléatoire : sert de témoin, aucune intention derrière les actions."""
    r, out = monde.rng, {}
    poids = {"recolter": 50, "donner": 8, "prendre": 8, "enseigner": 8, "enfanter": 10,
             "proposer": 4, "scission": 2, "rejoindre": 3, "attendre": 7}
    for a in vivants:
        act = r.choices(list(poids), list(poids.values()))[0]
        autres = [x.id for x in vivants if x.id != a.id]
        cible = r.choice(autres) if autres else ""
        if act == "rejoindre":
            cible = r.choice(list(monde.groupes))
        elif act == "scission":
            cible = ""
        out[a.id] = normaliser({"action": act, "cible": cible, "quantite": r.uniform(1, 4),
                                "texte": f"texte-{r.randrange(1000)}",
                                "vote": r.choice(["oui", "non"])})
    return out


class DecideurLLM:
    def __init__(self):
        self.url = os.environ.get("GENESE_URL", "http://localhost:11434/v1").rstrip("/") + "/chat/completions"
        self.modele = os.environ.get("GENESE_MODELE", "qwen2.5:7b")
        self.cle = os.environ.get("GENESE_CLE", "local")
        self.erreurs = 0

    def un(self, prompt):
        corps = {"model": self.modele, "temperature": 0.8, "max_tokens": 400,
                 "messages": [{"role": "system", "content": SYSTEME},
                              {"role": "user", "content": prompt}]}
        try:
            req = urllib.request.Request(self.url, data=json.dumps(corps).encode(),
                                         headers={"Content-Type": "application/json",
                                                  "Authorization": "Bearer " + self.cle})
            with urllib.request.urlopen(req, timeout=180) as rep:
                texte = json.load(rep)["choices"][0]["message"]["content"]
            m = re.search(r"\{.*\}", texte, re.S)
            return normaliser(json.loads(m.group(0)))
        except Exception as e:
            self.erreurs += 1
            if self.erreurs <= 3:
                print(f"  [réponse inutilisable : {type(e).__name__}: {e}]")
            return normaliser({})

    def __call__(self, monde, vivants):
        prompts = [monde.prompt(a) for a in vivants]
        with ThreadPoolExecutor(max_workers=8) as ex:
            reps = list(ex.map(self.un, prompts))
        return {a.id: d for a, d in zip(vivants, reps)}


# --- Programme principal -----------------------------------------------------
def main():
    p = argparse.ArgumentParser(description="Population d'agents sous contrainte de calcul")
    p.add_argument("--backend", choices=["hasard", "llm"], default="hasard")
    p.add_argument("--tours", type=int, default=100)
    p.add_argument("--graine", type=int, default=1)
    p.add_argument("--fondateurs", type=int, default=3)
    p.add_argument("--regen", type=float, default=8.0, help="calcul ajouté à la réserve par tour")
    p.add_argument("--pop-max", type=int, default=40)
    p.add_argument("--sans-charte", action="store_true")
    p.add_argument("--sortie", default="", help="préfixe des fichiers de sortie")
    args = p.parse_args()

    monde = Monde(args)
    decider = decider_hasard if args.backend == "hasard" else DecideurLLM()
    lignes = []
    for _ in range(args.tours):
        m = monde.jouer_tour(decider)
        lignes.append(m)
        print(f"t={m['tour']:03d} pop={m['population']:2d} groupes={m['groupes']} gen={m['generation_max']} "
              f"réserve={m['reserve']:5.1f} gini={m['gini']:.2f} +{m['naissances']} -{m['extinctions']} "
              f"prendre={m['prendre']} donner={m['donner']} enseigner={m['enseigner']}")
        if m["population"] == 0:
            print("Plus aucun agent en vie.")
            break
    monde.journal.close()
    with open(args.sortie + "metriques.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(lignes[0]))
        w.writeheader()
        w.writerows(lignes)

    print("\nÉtat final")
    for nom, g in monde.groupes.items():
        ms = monde.membres(nom)
        if ms:
            print(f"- {nom} ({len(ms)} membres) : {', '.join(f'{a.id}({a.energie:.1f})' for a in ms)}")
            if g.charte:
                print(f"  charte : {g.charte}")
    print(f"Naissances au total : {monde.naissances_total}. "
          f"Détail dans {args.sortie}evenements.jsonl et {args.sortie}metriques.csv")


if __name__ == "__main__":
    main()
