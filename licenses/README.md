# Windows package dependencies

- CascLib 3.0 by Ladislav Zezula, under the MIT license. Built with its bundled
  zlib 1.2.11 by Jean-loup Gailly and Mark Adler.
  [CascLib source and license](https://github.com/ladislav-zezula/CascLib/tree/3.0)
- DBCD and DBCD.IO by wowdev, under the MIT license.
  [DBCD source](https://github.com/wowdev/DBCD/tree/e732093f8864240fc5884bd1bba6b02f3dfc0d56)
- DBDefsLib 1.1.0 by WoWDev, declared MIT in the NuGet package.
  [DBDefsLib on NuGet](https://www.nuget.org/packages/DBDefsLib/1.1.0)
- WoWDBDefs database definitions by WoWDBDefs Contributors, under CC BY-SA 4.0.
  The eight included DBD files are unmodified.
  [Definitions source](https://github.com/wowdev/WoWDBDefs/tree/005c13a9a101e64014eeb02af3a42ccbeaf8513d/definitions)
- Microsoft .NET Runtime 10.0.9, bundled in Db2Export.exe.
  [Runtime package and license](https://www.nuget.org/packages/Microsoft.NETCore.App.Runtime.win-x64/10.0.9)
- Recast Navigation 1.6.0 Detour, linked into `route_nav_3.dll` with 64-bit
  polygon references. The license is included in `native/DETOUR_LICENSE.txt`.
  [Recast Navigation source](https://github.com/recastnavigation/recastnavigation/tree/v1.6.0)

Setup retrieves Python packages from PyPI and builds the atlas from the installed
WoW client. Classic setup downloads its mesh generators directly from the pinned
[CMaNGOS Classic release](https://github.com/cmangos/mangos-classic/releases/download/latest/mangos-classic-with-all-28a26c38.zip).
The package includes the native adapters and atlas metadata. Game artwork and
generated meshes are created on the user's PC.
