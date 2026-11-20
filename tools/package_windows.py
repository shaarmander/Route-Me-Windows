"""Stage and zip the Windows distribution from app source and compiled dependencies."""
import argparse
import shutil
import zipfile
from pathlib import Path

from tools.setup_forever import MAP_TABLES

ROOT = Path(__file__).resolve().parents[1]
SOURCE = (
    'app.py', 'atlas.py', 'atlas_browser.py', 'continent_routing.py', 'core.py',
    'desktop_overlay.py', 'diagnostics.py', 'map_profiles.py', 'movement.py',
    'navigation.py', 'overlay_maps.py', 'profile_navigation.py', 'projection.py',
    'road_curves.py', 'road_editor.py', 'road_network.py', 'settings.py',
    'telemetry.py', 'tracking.py', 'viewport.py', 'win_input.py', 'zone_maps.py',
    'requirements.txt', 'requirements-forever.txt', 'run.ps1', 'setup.ps1', 'Launch Route Me.cmd',
    'Setup Route Me.cmd', 'Install Start Menu.ps1',
    'addon/Wayfinder/Wayfinder.lua', 'addon/Wayfinder/Wayfinder.toc',
    'addon/Wayfinder/PROTOCOL.md', 'assets/maps/atlas.json', 'assets/maps/catalog.json',
    'tools/Db2Export/Db2Export.csproj', 'tools/Db2Export/Program.cs',
    'tools/build_client_map_profile.py', 'tools/extract_client_assets.py',
    'tools/migrate_client_roads.py', 'tools/setup_forever.py', 'tools/package_windows.py',
)


def package(stage, archive, dotnet_runtime, notices):
    stage.mkdir(parents=True)
    # Only the distribution allowlist enters the staging directory.
    inputs = {name: ROOT / name for name in SOURCE}
    inputs['README.md'] = ROOT / 'WINDOWS_INSTALL.md'
    inputs['WINDOWS_INSTALL.md'] = ROOT / 'WINDOWS_INSTALL.md'
    deps = ROOT / 'native/build/client-extraction'
    inputs.update({
        'runtime/CascLib.dll': ROOT / 'dist/build/casc/Release/CascLib.dll',
        'runtime/Db2Export.exe': ROOT / 'dist/build/db2-export/Db2Export.exe',
        'licenses/CascLib.txt': deps / 'CascLib-3.0/LICENSE',
        'licenses/DBCD.txt': deps / 'DBCD-e732093f8864240fc5884bd1bba6b02f3dfc0d56/LICENSE',
        'licenses/DotNet.txt': dotnet_runtime / 'LICENSE.TXT',
        'licenses/DotNet-Third-Party.txt': dotnet_runtime / 'THIRD-PARTY-NOTICES.TXT',
        'licenses/WoWDBDefs.md': notices / 'WoWDBDefs.md',
        'licenses/DBDefsLib.md': notices / 'DBDefsLib.md',
        'licenses/README.md': ROOT / 'tools/WINDOWS_DEPENDENCIES.md',
    })
    for name in MAP_TABLES:
        inputs[f'runtime/definitions/{name}.dbd'] = deps / 'definitions' / f'{name}.dbd'
    inputs['tools/WINDOWS_DEPENDENCIES.md'] = ROOT / 'tools/WINDOWS_DEPENDENCIES.md'
    for name, source in inputs.items():
        destination = stage / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    with zipfile.ZipFile(archive, 'x', zipfile.ZIP_DEFLATED, compresslevel=6) as output:
        for name in sorted(inputs):
            output.write(stage / name, f'Route Me/{name}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('stage', type=Path)
    parser.add_argument('archive', type=Path)
    parser.add_argument('dotnet_runtime', type=Path)
    parser.add_argument('notices', type=Path)
    args = parser.parse_args()
    package(args.stage, args.archive, args.dotnet_runtime, args.notices)
