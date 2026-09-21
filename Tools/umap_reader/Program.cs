// Reads a level's .umap straight off disk and writes everything it stores as JSON.
//
//     dotnet run -c Release -- <project dir> <level name> <output file>
//
// This exists because the engine's own exporters drop things the port needs: actor GUIDs are a bare
// UPROPERTY() and so unreadable from Python, the .t3d exporter unbinds dynamic delegates before writing,
// and it skips foliage entirely. None of that applies here -- a package is read as stored, and its objects
// are addressed by export index rather than by name, which matters because component names repeat.
//
// What a level stores is only what differs from the class defaults, so this file is the level's own
// configuration and nothing else; the inherited values come from the engine-side export. No filtering is
// done here on purpose: whatever the file holds, this writes.
using System; using System.IO; using System.Linq; using System.Collections.Generic;
using CUE4Parse.FileProvider; using CUE4Parse.UE4.Assets; using CUE4Parse.UE4.Assets.Objects;
using CUE4Parse.UE4.Versions;
using Newtonsoft.Json;

if (args.Length != 3)
{
    Console.Error.WriteLine("usage: umap_reader <project dir> <level name, e.g. L_Swamp_01> <output .json>");
    return 2;
}
var (projectDir, levelName, outputPath) = (args[0], args[1], args[2]);

var provider = new DefaultFileProvider(new DirectoryInfo(projectDir), SearchOption.AllDirectories,
    new VersionContainer(EGame.GAME_UE5_7), StringComparer.OrdinalIgnoreCase);
provider.Initialize();

var key = provider.Files.Keys.FirstOrDefault(k => k.EndsWith($"{levelName}.umap", StringComparison.OrdinalIgnoreCase));
if (key is null)
{
    Console.Error.WriteLine($"no {levelName}.umap under {projectDir}");
    return 1;
}

// Structs and arrays are expanded in place; anything else is left as the text the reader gives us.
// A static array writes one entry per index, hence the "#n" suffix on names.
static object? Value(object? raw)
{
    if (raw is FScriptStruct wrapper) raw = wrapper.StructType;
    if (raw is FStructFallback strct)
        return strct.Properties.ToDictionary(p => $"{p.Name.Text}#{p.ArrayIndex}", p => Value(p.Tag?.GenericValue));
    if (raw is UScriptArray array) return array.Properties.Select(p => Value(p.GenericValue)).ToList();
    if (raw is UScriptMap map)
        return map.Properties.Select(kv => new Dictionary<string, object?>
            { ["key"] = Value(kv.Key?.GenericValue), ["value"] = Value(kv.Value?.GenericValue) }).ToList();
    return raw?.ToString();
}

// Names repeat across a level -- every Blueprint actor has its own "DefaultSceneRoot" -- so each object
// also carries the chain of names that leads to it, which is what identifies it.
static string ObjectPath(CUE4Parse.UE4.Assets.Exports.UObject export)
{
    var chain = new List<string> { export.Name };
    for (var outer = export.Outer; outer is not null; outer = outer.Outer)
        chain.Insert(0, outer.Name.Text);
    return string.Join('.', chain);
}

var exports = ((IPackage)provider.LoadPackage(key)).GetExports().ToList();
var objects = exports.Select((export, index) => new
{
    index,
    name = export.Name,
    path = ObjectPath(export),
    @class = export.Class?.Name,
    outer = export.Outer?.Name,
    properties = export.Properties.Select(p => new
    {
        name = p.Name.Text,
        arrayIndex = p.ArrayIndex,
        value = Value(p.Tag?.GenericValue),
    }).ToList(),
}).ToList();

Directory.CreateDirectory(Path.GetDirectoryName(Path.GetFullPath(outputPath))!);
File.WriteAllText(outputPath, JsonConvert.SerializeObject(new
{
    level = levelName,
    package = key,
    objects,
}, Formatting.Indented));

Console.WriteLine($"{key}: {objects.Count} objects, {objects.Sum(o => o.properties.Count)} stored properties -> {outputPath}");
return 0;
