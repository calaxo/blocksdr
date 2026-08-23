"""Le graphe de blocs : construction, validation, exécution.

Modèle repris de GNU Radio, en beaucoup plus simple : on trie les blocs dans
l'ordre topologique, puis à chaque tour on les exécute une fois dans cet ordre
en passant les signaux de sortie aux entrées suivantes.

Ce qu'on ne fait PAS (et qui explique la simplicité) : pas d'ordonnanceur à
files d'attente par arête, pas de débits différents par branche. Chaque bloc
consomme le bloc qu'on lui donne et rend ce qu'il produit. Suffisant tant qu'on
reste sur des chaînes « un tour = un morceau de flux ».
"""

from __future__ import annotations

import time
from typing import Any

from .blocks import REGISTRY, Block, Ctx, Signal


class GraphError(Exception):
    pass


DEFAULT_GRAPH: dict[str, Any] = {
    "nodes": [
        {"id": "src", "type": "sim_source", "x": 40, "y": 180, "params": {}},
        {"id": "dec", "type": "decimate", "x": 330, "y": 180, "params": {"factor": 1}},
        {"id": "fft", "type": "fft", "x": 610, "y": 180, "params": {}},
        {"id": "wf", "type": "waterfall_sink", "x": 890, "y": 110, "params": {}},
        {"id": "spec", "type": "spectrum_sink", "x": 890, "y": 300, "params": {}},
    ],
    "edges": [
        {"from": "src", "fromPort": "out", "to": "dec", "toPort": "in"},
        {"from": "dec", "fromPort": "out", "to": "fft", "toPort": "in"},
        {"from": "fft", "fromPort": "out", "to": "wf", "toPort": "in"},
        {"from": "fft", "fromPort": "out", "to": "spec", "toPort": "in"},
    ],
}


class Flowgraph:
    def __init__(self, spec: dict[str, Any]):
        self.spec = spec
        self.blocks: dict[str, Block] = {}
        self.order: list[str] = []
        self.edges: list[dict] = list(spec.get("edges") or [])
        self._build()

    # -- construction ------------------------------------------------------
    def _build(self) -> None:
        nodes = self.spec.get("nodes") or []
        if not nodes:
            raise GraphError("Le graphe est vide.")

        for n in nodes:
            t = n.get("type")
            cls = REGISTRY.get(t)
            if cls is None:
                raise GraphError(f"Bloc inconnu : « {t} »")
            self.blocks[n["id"]] = cls(n["id"], n.get("params") or {})

        for e in self.edges:
            for side in ("from", "to"):
                if e.get(side) not in self.blocks:
                    raise GraphError(f"Liaison vers un bloc absent : {e.get(side)}")
            src, dst = self.blocks[e["from"]], self.blocks[e["to"]]
            if e.get("fromPort") not in [p["name"] for p in src.outputs]:
                raise GraphError(f"{src.label} n'a pas de sortie « {e.get('fromPort')} »")
            if e.get("toPort") not in [p["name"] for p in dst.inputs]:
                raise GraphError(f"{dst.label} n'a pas d'entrée « {e.get('toPort')} »")

        # Une entrée ne peut recevoir qu'une seule liaison (une sortie, elle,
        # peut en alimenter plusieurs — c'est le cas du FFT vers deux afficheurs).
        seen = set()
        for e in self.edges:
            key = (e["to"], e["toPort"])
            if key in seen:
                raise GraphError(
                    f"L'entrée « {e['toPort'] } » de {self.blocks[e['to']].label} "
                    "reçoit déjà une liaison.")
            seen.add(key)

        self.order = self._topo()

        errs = [f"{b.label} : {b.error}" for b in self.blocks.values() if b.error]
        if errs:
            raise GraphError(" · ".join(errs))

    def _topo(self) -> list[str]:
        """Tri topologique (Kahn). Détecte les boucles, interdites ici."""
        deps: dict[str, set[str]] = {k: set() for k in self.blocks}
        for e in self.edges:
            deps[e["to"]].add(e["from"])
        order, ready = [], [k for k, d in deps.items() if not d]
        ready.sort()
        while ready:
            k = ready.pop(0)
            order.append(k)
            for other, d in deps.items():
                if k in d:
                    d.discard(k)
                    if not d and other not in order and other not in ready:
                        ready.append(other)
            ready.sort()
        if len(order) != len(self.blocks):
            reste = ", ".join(sorted(set(self.blocks) - set(order)))
            raise GraphError(f"Boucle détectée dans le graphe (blocs : {reste}). "
                             "Un flux doit aller de la source vers les afficheurs.")
        return order

    # -- exécution ---------------------------------------------------------
    def tick(self, ctx: Ctx) -> None:
        vals: dict[tuple[str, str], Signal] = {}
        incoming = {(e["to"], e["toPort"]): (e["from"], e["fromPort"]) for e in self.edges}
        for nid in self.order:
            blk = self.blocks[nid]
            ins: dict[str, Signal] = {}
            for port in [p["name"] for p in blk.inputs]:
                src = incoming.get((nid, port))
                if src and src in vals:
                    ins[port] = vals[src]
            out = blk.work(ins, ctx) or {}
            for port, sig in out.items():
                vals[(nid, port)] = sig

    def period(self) -> float:
        """Durée réelle que représente un tour, pour cadencer la boucle.

        Sans ça, une source simulée tournerait à la vitesse du processeur et
        produirait des heures de signal par seconde. C'est le rôle du bloc
        « Throttle » de GNU Radio ; ici c'est l'ordonnanceur qui s'en charge,
        d'après la source la plus lente.
        """
        best = 0.05
        for b in self.blocks.values():
            sr = float(b.p.get("sample_rate") or 0)
            n = float(b.p.get("block_size") or 0)
            if sr > 0 and n > 0:
                best = min(best, n / sr) if best != 0.05 else n / sr
        return max(0.001, best)

    def close(self) -> None:
        for b in self.blocks.values():
            try:
                b.close()
            except Exception:
                pass
