"""Forme des requêtes acceptées par l'API, vérifiée par pydantic.

Deux étages de vérification, volontairement séparés :

  1. ICI, la **structure** : bons champs, bons types, tailles bornées. Rien
     de ce qui est refusé à ce stade ne peut venir du front tel qu'il est écrit
     — c'est un client bricolé ou malveillant, et il reçoit un 422.
  2. Dans `blocks.check_param` et `Flowgraph`, le **sens** : bloc connu,
     paramètre dans ses bornes, liaisons cohérentes. Ces erreurs-là sont
     normales pendant l'édition, et reviennent en message lisible
     (`{"ok": false, "error": …}`), comme avant.
"""

from __future__ import annotations

from typing import Annotated, Any, Union

from pydantic import (BaseModel, ConfigDict, Field, StrictBool, StrictFloat,
                      StrictInt, StringConstraints)

# Assez pour tous les graphes raisonnables ; une borne, surtout, pour qu'un
# graphe de 100 000 blocs ne monopolise pas le processeur.
MAX_NODES = 64
MAX_EDGES = 128
MAX_PARAMS = 32

# Identifiants de blocs, de types et de ports : ceux du front sont de la forme
# `fft_1a2b3c` ou `in` / `out`.
Ident = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.-]{1,64}$")]

# Une valeur de paramètre : jamais d'objet ni de liste.
ParamValue = Union[StrictBool, StrictInt, StrictFloat,
                   Annotated[str, StringConstraints(max_length=64)]]

Coord = Annotated[float, Field(ge=-1e6, le=1e6, allow_inf_nan=False)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NodeIn(_Strict):
    id: Ident
    type: Ident
    x: Coord = 0
    y: Coord = 0
    params: Annotated[dict[Ident, ParamValue], Field(max_length=MAX_PARAMS)] = {}


class EdgeIn(_Strict):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    from_: Ident = Field(alias="from")
    fromPort: Ident
    to: Ident
    toPort: Ident


class GraphIn(_Strict):
    nodes: Annotated[list[NodeIn], Field(max_length=MAX_NODES)]
    edges: Annotated[list[EdgeIn], Field(max_length=MAX_EDGES)]

    def to_spec(self) -> dict[str, Any]:
        """Le dictionnaire que consomme `Flowgraph`, au format historique."""
        return {"nodes": [n.model_dump() for n in self.nodes],
                "edges": [e.model_dump(by_alias=True) for e in self.edges]}


class RunIn(_Strict):
    running: StrictBool
