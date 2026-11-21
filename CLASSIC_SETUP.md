# WoW 1.12.1 collision preview

Route Me checks outdoor routes against terrain and static obstacles extracted
from the original WoW 1.12.1 client, build **5875**. It previews the checked
path on the desktop map. The stock client uses the offline preview workflow.
Wayfinder and **Connect** are for clients with compatible live telemetry.

## Install from your client

1. Install [64-bit Python 3.13 for Windows](https://www.python.org/downloads/windows/)
   with the Python launcher, pip, and Tcl/Tk. Download and extract
   [Route-Me-Windows-x64.zip](https://github.com/shaarmander/Route-Me-Windows/releases/latest)
   into a writable folder.
2. Run **Setup Classic 1.12.cmd** and select the folder containing your original
   `WoW.exe` and `Data` directory. Setup imports map artwork from that client.
   It uses compatible local `dbc` and `mmaps` if present. On a fresh client,
   setup downloads the CMaNGOS extractors and generates terrain and mesh files
   under `client-data.local`. Mesh generation for both continents can take hours.
   The CMaNGOS tools require the [Microsoft Visual C++ x64 Redistributable](https://aka.ms/vc14/vc_redist.x64.exe).
3. Open **Route Me**, select **1.12 Classic** in **Maps**, and choose an outdoor
   zone such as Dun Morogh. Select **Draw trip**, click a starting point on
   walkable ground, add guide points, and double-click the endpoint. Green
   shading shows the walkable mesh and the cyan line shows the checked route.
   If the endpoints are blocked or the path can't be verified, edit the guide
   and try again.

## Use existing extracted files

In PowerShell from the Route Me folder, pass the client path and the directory
containing the extracted data:

```powershell
& '.\Setup Classic 1.12.cmd' -Client 'D:\Games\WoW 1.12.1' -DataRoot 'D:\WoW Classic meshes'
```

The above invocation installs the Classic maps and activates collision preview.
Your extraction directory should contain:

```text
dbc/
  WorldMapArea.dbc
  WorldMapOverlay.dbc
mmaps/
  000.mmap          Eastern Kingdoms
  000*.mmtile
  001.mmap          Kalimdor
  001*.mmtile
```

An extraction with one continent can be used for that continent. The reader
expects **CMaNGOS mmap v8, Detour v7, and 64-bit polygon references**. Raw
`maps` and `vmaps` are intermediate inputs for generating the `mmaps`.
To build a fresh extraction, run **Setup Classic 1.12.cmd -GenerateMeshes**.
The [CMaNGOS x64 archive used by setup](https://github.com/cmangos/mangos-classic/releases/download/latest/mangos-classic-with-all-28a26c38.zip)
has the four tools under `x64_RelWithDebInfo/Extractors`. You can also
[run those tools yourself](https://github.com/cmangos/mangos-classic/blob/28a26c388a3c817f34cdb4737847a345841b6a08/contrib/extractor_scripts/README.txt).

The release supplies `runtime/route_nav_3.dll`. To build the adapter from source,
install CMake and Visual Studio 2022 with Desktop development with C++, unpack
[Recast Navigation v1.6.0](https://github.com/recastnavigation/recastnavigation/releases/tag/v1.6.0),
and run:

```powershell
& '.\Build Navigation.ps1' -DetourRoot 'D:\src\recastnavigation-1.6.0\Detour'
```
