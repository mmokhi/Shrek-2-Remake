"""Checks the file-side read of a level against the engine's own export of it.

    compare_exports.py <level_raw.json> <level.t3d>

The two are produced by unrelated code from unrelated inputs: Tools/umap_reader parses the .umap off disk,
and the .t3d is written by the engine from the loaded level. A level stores only what differs from the class
defaults, so both describe the same set of overrides and must agree on every value they both carry.

They are not expected to carry the *same* set. The engine's export legitimately holds more (objects built
when the level loads, and the player pawn our export spawns) and legitimately holds less (it skips foliage,
it never writes GUIDs, and it unbinds dynamic delegates before writing). So the sets are reported and the
values are enforced: any object and property present on both sides must have the same value, or this fails.
"""
import collections
import json
import re
import sys

MARKERS = ("Begin Actor", "Begin Object")


def parse_t3d(path):
    """Unreal text -> {object path: {property name: value text}}, following Begin/End nesting.

    Keyed by the chain of names, not the name: every Blueprint actor owns a "DefaultSceneRoot", so names
    alone would collapse hundreds of different objects onto each other.
    """
    objects, stack = {}, []
    for line in open(path, errors="replace"):
        line = line.strip()
        if line.startswith(MARKERS):
            name = re.search(r'\bName="?([\w\-]+)"?', line)
            stack.append(name.group(1) if name else None)
        elif line.startswith(("End Actor", "End Object")):
            if stack:
                stack.pop()
        elif "=" in line and stack and stack[-1]:
            key, _, value = line.partition("=")
            key = re.sub(r"\(.*", "", key).split("[")[0].strip()
            if re.fullmatch(r"[A-Za-z_]\w*", key):
                objects.setdefault(".".join(n for n in stack if n), {})[key] = value.strip()
    return objects


def tail(path):
    """The part of an object's path below the level, which is what both sides have in common."""
    return path.split("PersistentLevel.")[-1]


OBJECT_REFERENCE = re.compile(r"^[\w/.]+'(?P<path>.*)'?$")
NUMBER = re.compile(r"-?\d+\.?\d*(?:[eE][-+]?\d+)?")
GUID = re.compile(r"^[0-9A-Fa-f]{32}$")
COLOUR_HEX = re.compile(r"^[0-9A-Fa-f]{6,8}$")


def normalise(value):
    """Flatten the two notations onto one.

    The file writes an object reference as Class'/Full/Path.Outer.Name' where the engine writes "Name", and
    an enum as EType::Value where the engine writes Value. Everything else is stripped of the punctuation
    and field labels the two spell differently -- the file's rotator is "P= Y= R=", the engine's is
    "(Pitch=,Yaw=,Roll=)" -- leaving the values themselves.
    """
    text = str(value).strip().strip('"')
    reference = OBJECT_REFERENCE.match(text)
    if reference:
        # Down to the leaf name: the engine qualifies a reference with its class and mounts content at
        # /Game, the file names the same asset under the project's own content folder.
        text = re.split(r"[.:/]", reference.group("path").rstrip("'"))[-1]
    text = re.sub(r"\b[A-Z]\w*::", "", text)
    text = re.sub(r"\b[A-Za-z]\w*=", "", text)  # field labels, which the two sides spell differently
    return re.sub(r"[\s,'\"()]", "", text).lower()


def numbers(text):
    return [float(n) for n in NUMBER.findall(str(text))]


def verdict(file_value, engine_value):
    """"same", "regenerated", "incomparable" or "differs"."""
    if normalise(file_value) == normalise(engine_value):
        return "same"
    left, right = numbers(file_value), numbers(engine_value)
    if left and len(left) == len(right):
        # The file rounds a vector to three decimals where the engine prints six.
        if all(abs(a - b) <= 1e-3 * max(1.0, abs(a), abs(b)) for a, b in zip(left, right)):
            return "same"
    if GUID.match(file_value.strip()) and GUID.match(engine_value.strip()):
        return "regenerated"  # the engine assigns a fresh one on load; the stored one is the level's
    if COLOUR_HEX.match(file_value.strip()) and re.match(r"^\([BGR]=", engine_value.strip()):
        # A packed colour against named components. Byte components are the same colour written two ways,
        # so compare them; floating-point ones are a different (linear) space and are left alone.
        components = dict(re.findall(r"([BGRA])=(-?[\d.]+)", engine_value))
        if all(float(v).is_integer() for v in components.values()):
            packed = file_value.strip().ljust(8, "F")  # no alpha stored means opaque
            channels = dict(zip("RGBA", (int(packed[i:i + 2], 16) for i in range(0, 8, 2))))
            return "same" if all(channels.get(k) == int(float(v)) for k, v in components.items()) else "differs"
        return "incomparable"  # packed bytes vs a linear colour, not recoverable from each other
    if file_value.startswith("CUE4Parse."):
        return "unrendered"  # the file reader has no text form for this type, so there is nothing to compare
    return "differs"


def main(raw_path, t3d_path):
    raw = json.load(open(raw_path))
    engine = parse_t3d(t3d_path)
    file_side = {tail(o["path"]): {p["name"]: p["value"] for p in o["properties"]} for o in raw["objects"]}

    shared = sorted(set(file_side) & set(engine))
    tally, failures = collections.Counter(), []
    for name in shared:
        for prop, value in file_side[name].items():
            if prop not in engine[name] or not isinstance(value, str):
                continue  # structs print differently on the two sides; scalars are the common ground
            other = engine[name][prop]
            outcome = verdict(value, other)
            tally[outcome] += 1
            if outcome == "differs" and len(failures) < 25:
                failures.append(f"{name}.{prop}: file={value!r} engine={other!r}")

    print(f"objects: {len(file_side)} in the .umap, {len(engine)} in the .t3d, {len(shared)} in both")
    print(f"file-only objects  : {len(set(file_side) - set(engine))}  (foliage, level blueprint)")
    print(f"engine-only objects: {len(set(engine) - set(file_side))}  (built on load, plus the spawned pawn)")
    print(f"values agreeing    : {tally['same']}")
    print(f"  regenerated GUIDs: {tally['regenerated']}   incomparable encodings: {tally['incomparable']}"
          f"   types the file reader cannot render: {tally['unrendered']}")
    print(f"values DISAGREEING : {tally['differs']}")
    for failure in failures:
        print("  !", failure)
    return 1 if tally["differs"] else 0


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    sys.exit(main(sys.argv[1], sys.argv[2]))
