"""Headless export of one level with vortechU/UnrealToGodot (Plugins/UnrealToGodot), plus the two things the tool
leaves out that this port needs: the player character with its animations, and the sounds.

    UnrealEditor-Cmd <project> -run=pythonscript -script=export.py      env: LEVEL (/Game/...), OUT (directory)

Output in OUT: the tool's models/, textures/, terrain/ and layout.json, plus
    animations/<name>.glb   every animation of the player's skeleton (glTF, one per sequence)
    sounds/<package>.wav    every sound the level and the player can play (source audio)
    blueprints.json         every Blueprint class the level uses: parent class and the values set on its defaults
    summary.json            counts, and what failed
"""
import json
import os

import unreal

import export_level_to_json
import export_static_meshes_to_gltf

LEVEL, OUT = os.environ["LEVEL"], os.environ["OUT"]
summary = {"failed": []}


def save(asset, path, export):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        export(asset, path)
    except Exception as e:
        summary["failed"].append(f"{asset.get_path_name()}: {e}")


def export_gltf(asset, path):
    options = unreal.GLTFExportOptions()
    export_static_meshes_to_gltf.configure_gltf_export_options(options, export_animations=True)  # the tool's settings
    options.set_editor_property("export_preview_mesh", False)
    result = unreal.GLTFExporter.export_to_gltf(asset, path, options, set())
    if not (result[0] if isinstance(result, tuple) else result):
        raise RuntimeError("glTF export failed")


def export_wav(asset, path):
    task = unreal.AssetExportTask()
    task.object, task.filename, task.automated, task.prompt = asset, path, True, False
    if not unreal.Exporter.run_asset_export_task(task):
        raise RuntimeError("export failed")


def project_dependencies(roots):
    """Every package under /Game or a project plugin that the roots depend on, recursively."""
    plugins = unreal.Paths.convert_relative_path_to_full(unreal.Paths.project_plugins_dir())
    mounts = tuple(["/Game/"] + [f"/{f[:-8]}/" for _, _, files in os.walk(plugins) for f in files if f.endswith(".uplugin")])
    registry = unreal.AssetRegistryHelpers.get_asset_registry()
    registry.search_all_assets(True)
    options = unreal.AssetRegistryDependencyOptions(include_soft_package_references=True, include_hard_package_references=True)
    seen, stack = set(), list(roots)
    while stack:
        package = stack.pop()
        if package not in seen and package.startswith(mounts):
            seen.add(package)
            stack += [str(p) for p in registry.get_dependencies(package, options) or []]
    return seen



def jsonable(value, depth=0):
    """An Unreal property value as plain JSON: assets become their package path, structs and containers recurse."""
    if depth > 4:
        return str(value)
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (unreal.Name, unreal.Text)):
        return str(value)
    if isinstance(value, unreal.Object):
        return value.get_path_name()
    if isinstance(value, unreal.Class):
        return value.get_path_name()
    if isinstance(value, (unreal.Array, unreal.Set, list, set)):
        return [jsonable(v, depth + 1) for v in value]
    if isinstance(value, unreal.Map):
        return {str(k): jsonable(v, depth + 1) for k, v in value.items()}
    if isinstance(value, unreal.StructBase):
        fields = {}
        for name in dir(value):
            if name.startswith("_"):
                continue
            try:
                fields[name] = jsonable(value.get_editor_property(name), depth + 1)
            except Exception:
                continue
        return fields or str(value)
    return str(value)


def class_defaults(generated_class):
    """The values set on a class's default object: the Blueprint's variables, and what it overrides from its parent."""
    default_object = unreal.get_default_object(generated_class)
    values = {}
    for name in dir(default_object):
        if name.startswith("_"):
            continue
        try:
            value = default_object.get_editor_property(name)
        except Exception:
            continue  # not a property, or not readable outside the editor UI
        if callable(value):
            continue
        values[name] = jsonable(value)
    parent = generated_class.get_super_class()
    return {"parent": parent.get_path_name() if parent else None, "values": values}


world = unreal.EditorLoadingAndSavingUtils.load_map(LEVEL)
actors = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)

# The player isn't placed in the level: the game mode spawns it at the PlayerStart. Do the same, so the tool
# exports and places it like any other actor.
game_mode = world.get_world_settings().get_editor_property("default_game_mode")
pawn_class = unreal.get_default_object(game_mode).get_editor_property("default_pawn_class")
start = next(a for a in actors.get_all_level_actors() if isinstance(a, unreal.PlayerStart))
pawn = actors.spawn_actor_from_class(pawn_class, start.get_actor_location(), start.get_actor_rotation())
skeleton = pawn.get_editor_property("mesh").get_skeletal_mesh_asset().get_editor_property("skeleton").get_path_name()
summary["player"] = pawn_class.get_path_name()

# The tool: meshes and textures, then the layout (which now includes the player).
summary["meshes_exported"], summary["meshes_failed"] = export_static_meshes_to_gltf.export_all_level_meshes(
    export_dir=f"{OUT}/models", export_animations=False, show_dialogs=False)
summary["layout"] = export_level_to_json.export_level_to_json(
    save_path=f"{OUT}/layout.json", show_dialogs=False, skip_existing_textures=True)

# The additions.
summary["animations"] = summary["sounds"] = 0
blueprints = {}
for package in sorted(project_dependencies([LEVEL, pawn_class.get_outer().get_name()])):
    asset = unreal.EditorAssetLibrary.load_asset(package)
    if isinstance(asset, unreal.AnimSequence) and asset.get_editor_property("skeleton").get_path_name() == skeleton:
        save(asset, f"{OUT}/animations/{asset.get_name()}.glb", export_gltf)
        summary["animations"] += 1
    elif isinstance(asset, unreal.SoundWave):
        save(asset, f"{OUT}/sounds{package}.wav", export_wav)
        summary["sounds"] += 1
    elif isinstance(asset, unreal.Blueprint):
        # The settings the game gives each class: what a coin is worth, how fast the player runs, ...
        try:
            blueprints[package] = class_defaults(unreal.load_class(None, package + "." + asset.get_name() + "_C"))
        except Exception as e:
            summary["failed"].append(f"{package}: {e}")

with open(f"{OUT}/blueprints.json", "w") as f:
    json.dump(blueprints, f, indent=1, sort_keys=True)
summary["blueprints"] = len(blueprints)
summary["blueprint_values"] = sum(len(b["values"]) for b in blueprints.values())

with open(f"{OUT}/summary.json", "w") as f:
    json.dump(summary, f, indent=1)
unreal.log(f"unreal-to-godot: {summary}")
