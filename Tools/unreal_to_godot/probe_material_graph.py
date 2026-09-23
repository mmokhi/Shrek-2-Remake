"""Does Unreal let Python read how material nodes are wired together?

    UnrealEditor-Cmd <project> -run=pythonscript -script=probe_material_graph.py     env: OUT (directory)

UnrealToGodot assumes it does not. Its code and its schema both say expression inputs are unreadable, so
it never attempts a graph walk and materials transfer at parameter level only. That claim is about the
`FExpressionInput` PROPERTIES, which are protected; MaterialEditingLibrary.GetInputsForMaterialExpression is
a BlueprintPure UFUNCTION that iterates the same pins and returns what is connected
(MaterialEditingLibrary.cpp:1262-1274). Nothing in it touches an editor window, so it should work headless.

This writes material_graph_probe.json: for every base material the project uses, how many nodes it has and
how many of their input pins resolve to another node, plus one graph in full. If the connection count is
zero the walk is impossible from here and a material compiler has to read the asset files instead.
"""
import json
import os

import unreal

OUT = os.environ["OUT"]
mel = unreal.MaterialEditingLibrary


def graph_of(material):
    """Every node of one material, with what feeds each of its inputs."""
    nodes = []
    for expression in mel.get_material_expressions(material) or []:
        inputs = mel.get_inputs_for_material_expression(material, expression) or []
        nodes.append({
            "node": type(expression).__name__,
            "input_names": [str(n) for n in mel.get_material_expression_input_names(expression) or []],
            "output_names": [str(n) for n in mel.get_material_expression_output_names(expression) or []],
            # The question this probe exists to answer: is this list ever non-empty?
            "inputs": [type(i).__name__ if i else None for i in inputs],
        })
    return nodes


# What drives the material's own outputs -- the roots a compiler would start from.
PROPERTIES = ["BASE_COLOR", "METALLIC", "SPECULAR", "ROUGHNESS", "EMISSIVE_COLOR", "OPACITY", "NORMAL"]

registry = unreal.AssetRegistryHelpers.get_asset_registry()
registry.search_all_assets(True)
materials = []
for data in registry.get_assets_by_class("Material", True) or []:
    path = str(data.package_name)
    if path.startswith("/Game/") or path.startswith("/ClimbingSystem/") or path.startswith("/CombatSystem/"):
        materials.append(path)

summary = {"materials": 0, "nodes": 0, "connections": 0, "roots_found": 0, "failed": []}
sample = None
for path in sorted(materials):
    try:
        material = unreal.EditorAssetLibrary.load_asset(path)
        if not isinstance(material, unreal.Material):
            continue
        nodes = graph_of(material)
        connections = sum(1 for n in nodes for i in n["inputs"] if i)
        summary["materials"] += 1
        summary["nodes"] += len(nodes)
        summary["connections"] += connections
        for name in PROPERTIES:
            prop = getattr(unreal.MaterialProperty, f"MP_{name}", None)
            if prop is not None and mel.get_material_property_input_node(material, prop):
                summary["roots_found"] += 1
        # Keep the richest graph seen, as evidence of what a compiler would have to work with.
        if sample is None or connections > sample["connections"]:
            sample = {"material": path, "connections": connections, "nodes": nodes}
    except Exception as e:
        summary["failed"].append(f"{path}: {e}")

with open(f"{OUT}/material_graph_probe.json", "w") as f:
    json.dump({"summary": summary, "sample": sample}, f, indent=1)
unreal.log(f"material-graph-probe: {summary}")
