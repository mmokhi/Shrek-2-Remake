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

	_attach_unreal_values(root)

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


## Puts what the level file stores onto the nodes, verbatim, as metadata: "unreal/<PropertyName>" per object,
## and "unreal/path" so each node says which Unreal object it came from.
##
## Nothing here is translated into Godot's own terms -- a collision channel stays the text Unreal wrote, it
## does not become a collision layer. The values are carried so that nothing is lost; deciding what the port
## should *do* with them is a separate step.
##
## Actors with no node of their own -- particle emitters, BSP brushes, the player starts -- get an empty
## Node3D, so their settings arrive with everything else instead of being dropped.
func _attach_unreal_values(root: Node) -> void:
	var raw_text := FileAccess.get_file_as_string(EXPORT + "/level_raw.json")
	var index_text := FileAccess.get_file_as_string(EXPORT + "/actor_index.json")
	if raw_text.is_empty() or index_text.is_empty():
		printerr("shrek_import: level_raw.json or actor_index.json is missing; no Unreal values attached")
		get_tree().quit(1)
		return

	var raw: Dictionary = JSON.parse_string(raw_text)
	var index: Array = JSON.parse_string(index_text)

	# Every object in the file by its path below the level, and each actor's own components gathered once
	# rather than searched for per actor.
	var by_path := {}
	var components := {}
	for object in raw["objects"]:
		var path := _below_level(object["path"])
		by_path[path] = object
		var cut := path.rfind(".")
		if cut != -1:
			components.get_or_add(path.substr(0, cut), []).append(object)

	var nodes_by_name := {}
	_collect(root, nodes_by_name)

	var attached := 0
	var created := 0
	for entry in index:
		var actor_path: String = _below_level(entry["path"])
		if not by_path.has(actor_path):
			continue  # the level stores nothing for it, so there is nothing to carry
		var node: Node = nodes_by_name.get(entry["label"])
		if node == null:
			node = Node3D.new()
			node.name = entry["label"]
			root.add_child(node)
			node.owner = root
			created += 1
		attached += _set_values(node, by_path[actor_path], components.get(actor_path, []))

	print("shrek_import: %d values from the level file attached, %d nodes created for actors with no mesh"
			% [attached, created])


## The path below PersistentLevel, which is the part the engine and the file agree on.
func _below_level(path: String) -> String:
	var marker := "PersistentLevel."
	var at := path.find(marker)
	return path.substr(at + marker.length()) if at != -1 else path


func _collect(node: Node, into: Dictionary) -> void:
	for child in node.get_children():
		var name := String(child.name)
		if not into.has(name):
			into[name] = child
		_collect(child, into)


## Everything the level stores for one actor, as a single "unreal" metadata entry:
##
##     {path, class, properties: {name: value}, components: {name: {name: value}}}
##
## One entry rather than one per property, because a metadata name has to be a plain identifier -- it cannot
## carry the property's own name and its component's -- and because this keeps each component's settings
## together, the way the level holds them.
func _set_values(node: Node, object: Dictionary, components: Array) -> int:
	var count := 0
	var properties := {}
	for property in object["properties"]:
		properties[property["name"]] = property["value"]
		count += 1

	var by_component := {}
	for component in components:
		var values := {}
		for property in component["properties"]:
			values[property["name"]] = property["value"]
			count += 1
		by_component[component["name"]] = values

	node.set_meta("unreal", {
		"path": object["path"],
		"class": object["class"],
		"properties": properties,
		"components": by_component,
	})
	return count


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
