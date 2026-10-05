"""Vérification des entrées : structure (pydantic) puis sens (blocs, bornes)."""

from __future__ import annotations

import copy

import pytest
from pydantic import ValidationError

from app.blocks import REGISTRY, ParamError, check_param
from app.graph import DEFAULT_GRAPH, Flowgraph, GraphError
from app.schema import MAX_NODES, GraphIn


def graph(**changes):
    g = copy.deepcopy(DEFAULT_GRAPH)
    g["nodes"][0]["params"].update(changes)
    return g


# --- structure ---------------------------------------------------------------

def test_graphe_par_defaut_accepte():
    spec = GraphIn.model_validate(DEFAULT_GRAPH).to_spec()
    assert spec["edges"][0]["from"] == "src"          # alias rendu tel quel
    Flowgraph(spec, hardware=False).close()


@pytest.mark.parametrize("mutation", [
    lambda g: g["nodes"][0].update(id="../etc"),                  # identifiant douteux
    lambda g: g["nodes"][0].update(id="x" * 65),                  # trop long
    lambda g: g["nodes"][0].update(evil=1),                       # champ inconnu
    lambda g: g["nodes"][0]["params"].update(scene={"a": 1}),     # objet en paramètre
    lambda g: g["nodes"][0]["params"].update(scene="x" * 65),     # chaîne trop longue
    lambda g: g["nodes"][0].update(x=float("inf")),
    lambda g: g["edges"][0].update(fromPort=None),
    lambda g: g.update(nodes=g["nodes"] * (MAX_NODES // 5 + 1)),   # trop de blocs
])
def test_structure_refusee(mutation):
    g = copy.deepcopy(DEFAULT_GRAPH)
    mutation(g)
    with pytest.raises(ValidationError):
        GraphIn.model_validate(g)


def test_run_exige_un_vrai_booleen():
    from app.schema import RunIn
    with pytest.raises(ValidationError):
        RunIn.model_validate({"running": "yes"})


# --- sens : paramètres -------------------------------------------------------

def decl(block_type, name):
    return next(d for d in REGISTRY[block_type].params if d["name"] == name)


@pytest.mark.parametrize("block, name, value", [
    ("sim_source", "sample_rate", 0),             # division par zéro, avant
    ("sim_source", "sample_rate", 1e12),
    ("sim_source", "block_size", 10_000_000),      # mémoire
    ("sim_source", "block_size", 1024.5),          # entier attendu
    ("sim_source", "block_size", True),            # un booléen n'est pas un nombre
    ("sim_source", "scene", "inconnue"),
    ("rtlsdr_source", "center_freq", 5e9),
    ("rtlsdr_source", "gain", "1000"),
    ("fft", "size", 3000),
    ("decimate", "factor", 0),
    ("audio_sink", "mute", "oui"),
    ("freq_shift", "offset_hz", float("nan")),
])
def test_parametre_refuse(block, name, value):
    with pytest.raises(ParamError):
        check_param("bloc", decl(block, name), value)


def test_parametres_normalises():
    assert check_param("b", decl("fft", "size"), "4096") == 4096       # option entière
    assert check_param("b", decl("rtlsdr_source", "gain"), 30) == "30"  # option chaîne
    assert check_param("b", decl("sim_source", "block_size"), 2048.0) == 2048
    assert check_param("b", decl("rtlsdr_source", "center_freq"), 100_000_000) == 1e8


def test_tous_les_nombres_sont_bornes():
    """Garde-fou pour les futurs blocs : pas de paramètre numérique sans borne."""
    for cls in REGISTRY.values():
        for d in cls.params:
            if d["type"] in ("int", "float", "freq"):
                lo, hi = d.get("bounds", (d.get("min"), d.get("max")))
                assert lo is not None and hi is not None, f"{cls.type}.{d['name']}"


def test_parametre_hors_bornes_message_lisible():
    with pytest.raises(GraphError, match="Taille de bloc"):
        Flowgraph(graph(block_size=10), hardware=False)


# --- sens : graphe -----------------------------------------------------------

def test_bloc_inconnu():
    g = copy.deepcopy(DEFAULT_GRAPH)
    g["nodes"][0]["type"] = "rm_rf"
    with pytest.raises(GraphError, match="Bloc inconnu"):
        Flowgraph(g, hardware=False)


def test_identifiants_en_double():
    g = copy.deepcopy(DEFAULT_GRAPH)
    g["nodes"][1]["id"] = "src"
    with pytest.raises(GraphError, match="même identifiant"):
        Flowgraph(g, hardware=False)


def test_boucle_refusee():
    g = copy.deepcopy(DEFAULT_GRAPH)
    g["nodes"].append({"id": "sh", "type": "freq_shift", "params": {}})
    g["edges"] += [{"from": "dec", "fromPort": "out", "to": "sh", "toPort": "in"}]
    g["edges"][0] = {"from": "sh", "fromPort": "out", "to": "dec", "toPort": "in"}
    with pytest.raises(GraphError, match="Boucle"):
        Flowgraph(g, hardware=False)


def test_graphe_refuse_rend_le_dongle(fake_lib):
    """Une liaison invalide APRÈS l'ouverture du dongle doit le rendre."""
    g = {"nodes": [{"id": "src", "type": "rtlsdr_source", "params": {}},
                   {"id": "fft", "type": "fft", "params": {}}],
         "edges": [{"from": "src", "fromPort": "nope", "to": "fft", "toPort": "in"}]}
    with pytest.raises(GraphError):
        Flowgraph(g)
    assert fake_lib.opened == 1 and fake_lib.closed == 1
