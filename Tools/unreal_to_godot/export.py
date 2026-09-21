"""Headless export of one level with vortechU/UnrealToGodot (Plugins/UnrealToGodot), plus the two things the tool
leaves out that this port needs: the player character with its animations, and the sounds.

    UnrealEditor-Cmd <project> -run=pythonscript -script=export.py      env: LEVEL (/Game/...), OUT (directory)

Output in OUT: the tool's models/, textures/, terrain/ and layout.json, plus
    <level>.t3d             what each placed actor overrides, as Unreal text
    objdump.log             the editor log, holding an unfiltered dump of the level's object tree
    animations/<name>.glb   every animation of the player's skeleton (glTF, one per sequence)
    sounds/<package>.wav    every sound the level and the player can play (source audio)
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


def run_export_task(asset, path):
    """Hands the asset to whichever exporter the engine registers for it (.wav for a sound, .t3d for a level)."""
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

# What each placed actor overrides. The layout above carries placement but no settings, and it skips actors
# with no mesh, so the interaction handlers and triggers never reach it at all. The engine's own level
# exporter writes all of them as Unreal text, under the names the .umap stores, and a level stores only what
# differs from the class defaults, so what lands here is exactly the per-instance configuration
# (ULevelExporterT3D, EditorExporters.cpp:584-596). One thing it leaves out: it unbinds dynamic delegates
# before writing (EditorExporters.cpp:717), so per-instance event wiring is not in this file.
level_text = f"{OUT}/{world.get_name()}.t3d"
save(world, level_text, run_export_task)
summary["level_text"] = os.path.getsize(level_text) if os.path.exists(level_text) else 0

# The same graph again, but through the console's `obj dump`, which has no property filter: it walks the
# level's whole object tree with ExportProperties and includes transient values (UnrealEngine.cpp:10253-10258).
# Two things the .t3d above cannot give us are in here: the actor GUIDs, which no exporter writes and Python
# cannot read (bare UPROPERTY), and the event wiring, which the .t3d exporter deliberately unbinds first.
# It writes to the editor log, so the log is copied out below and parsed afterwards.
unreal.SystemLibrary.execute_console_command(
    world, f"obj dump class=World name={world.get_name()} recurse=true")

# The additions.
summary["animations"] = summary["sounds"] = 0
for package in sorted(project_dependencies([LEVEL, pawn_class.get_outer().get_name()])):
    asset = unreal.EditorAssetLibrary.load_asset(package)
    if isinstance(asset, unreal.AnimSequence) and asset.get_editor_property("skeleton").get_path_name() == skeleton:
        save(asset, f"{OUT}/animations/{asset.get_name()}.glb", export_gltf)
        summary["animations"] += 1
    elif isinstance(asset, unreal.SoundWave):
        save(asset, f"{OUT}/sounds{package}.wav", run_export_task)
        summary["sounds"] += 1

# The dump went to the log. Take the dump out of it -- from its first marker to the end -- rather than the
# whole log, which the workflow already ships on its own.
MARKER = "*** Property dump for object"
log_dir = unreal.Paths.convert_relative_path_to_full(unreal.Paths.project_log_dir())
logs = [os.path.join(log_dir, f) for f in os.listdir(log_dir) if f.endswith(".log")] if os.path.isdir(log_dir) else []
summary["objdump_markers"] = summary["objdump_bytes"] = 0
if not logs:
    summary["failed"].append(f"no editor log in {log_dir}")
else:
    with open(max(logs, key=os.path.getmtime), "r", errors="replace") as src:
        text = src.read()
    start = text.find(MARKER)
    if start < 0:
        summary["failed"].append("obj dump wrote nothing to the log")
    else:
        dump = text[start:]
        with open(f"{OUT}/objdump.log", "w") as dst:
            dst.write(dump)
        summary["objdump_markers"] = dump.count(MARKER)
        summary["objdump_bytes"] = len(dump)

with open(f"{OUT}/summary.json", "w") as f:
    json.dump(summary, f, indent=1)
unreal.log(f"unreal-to-godot: {summary}")
