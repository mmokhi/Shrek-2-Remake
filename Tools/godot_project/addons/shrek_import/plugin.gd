@tool
extends EditorPlugin
## Imports the exported level in res://unreal/ and saves it as a scene, using the UnrealToGodot importer addon.
##
## Godot cannot run an EditorScript from the command line and that importer is one, so this runs the import
## instead: editor plugins do load in a headless editor. With SHREK_IMPORT=1 it imports at startup and quits,
## which is how CI builds the project; in a normal editor session it does nothing.

const EXPORT := "res://unreal"

const Importer := preload("res://addons/unreal_importer/import_unreal_layout.gd")


func _enter_tree() -> void:
	if OS.get_environment("SHREK_IMPORT") == "1":
		_import.call_deferred()


func _import() -> void:
	var layout: Dictionary = JSON.parse_string(FileAccess.get_file_as_string(EXPORT + "/layout.json"))
	var level: String = layout.get("level_name", "Level")

	var root := Node3D.new()
	root.name = level
	var ok: bool = await Importer.new().do_import(
		EXPORT + "/layout.json", EXPORT + "/models", EXPORT + "/textures", root,
		{"terrain_mode": "mesh"})  # Terrain3D is not installed; its mesh fallback needs no plugin
	if not ok:
		printerr("shrek_import: the importer reported failure")
		get_tree().quit(1)
		return

	# A saved scene only stores what it owns. The importer puts its materials on the mesh nodes inside each
	# instanced model, which belong to that model's scene, so packing as-is would drop them and leave the
	# glTF's own untextured materials. Writing every node into the level keeps them.
	var textured := _flatten(root, root)

	var scene := PackedScene.new()
	if scene.pack(root) != OK:
		printerr("shrek_import: could not pack the scene")
		get_tree().quit(1)
		return
	DirAccess.make_dir_recursive_absolute("res://scenes")
	var path := "res://scenes/%s.tscn" % level
	if ResourceSaver.save(scene, path) != OK:
		printerr("shrek_import: could not save ", path)
		get_tree().quit(1)
		return

	# So the project opens and plays the level straight away.
	ProjectSettings.set_setting("application/run/main_scene", path)
	ProjectSettings.save()
	print("shrek_import: %s built, %d top-level nodes, %d textured surfaces" % [path, root.get_child_count(), textured])
	get_tree().quit(0)


## Makes every node part of the level scene rather than of an instanced model, and reports how many surfaces
## carry a texture, which is what the saved scene would lose if the nodes stayed inside their instances.
func _flatten(node: Node, root: Node) -> int:
	var textured := 0
	for child in node.get_children():
		child.owner = root
		child.scene_file_path = ""
		if child is MeshInstance3D and child.mesh != null:
			for surface in child.mesh.get_surface_count():
				var material: Material = child.get_surface_override_material(surface)
				if material == null:
					material = child.mesh.surface_get_material(surface)
				if material is BaseMaterial3D and material.albedo_texture != null:
					textured += 1
		textured += _flatten(child, root)
	return textured
