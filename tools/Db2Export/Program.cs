using System.Text.Json;
using DBCD;
using DBCD.Providers;

if (args.Length < 5)
{
    Console.Error.WriteLine("Usage: Db2Export <db2-directory> <dbd-directory> <client-version> <output-directory> <table> [...]");
    return 2;
}
var reader = new DBCD.DBCD(new FilesystemDBCProvider(args[0]), new FilesystemDBDProvider(args[1]));
Directory.CreateDirectory(args[3]);
foreach (var name in args.Skip(4))
{
    var storage = reader.Load(name, args[2]);
    var records = new List<Dictionary<string, object?>>();
    foreach (var id in storage.Keys.Order())
    {
        DBCDRow row = storage[id];
        var record = new Dictionary<string, object?> { ["ID"] = id };
        foreach (var field in row.GetDynamicMemberNames())
            record[field] = row[field];
        records.Add(record);
    }
    var output = Path.Combine(args[3], name + ".json");
    File.WriteAllText(output, JsonSerializer.Serialize(records, new JsonSerializerOptions { WriteIndented = true }));
    Console.WriteLine($"{name}: {records.Count} records");
}
return 0;
