"""Import WoW: Forever maps from the local installation for the Windows package."""
import argparse
import subprocess
from pathlib import Path

from map_profiles import available_catalogs, remember_catalog
from tools.build_client_map_profile import build
from tools.extract_client_assets import DEFAULT_PRODUCT, ROOT, extract, installed_build
from tools.migrate_client_roads import migrate

MAP_TABLES = (
    'UiMap', 'UiMapAssignment', 'UiMapArt', 'UiMapArtStyleLayer',
    'UiMapArtTile', 'UiMapXMapArt', 'WorldMapOverlay', 'WorldMapOverlayTile',
)


def setup(client):
    product = DEFAULT_PRODUCT
    version = installed_build(client, product)
    number = int(version.rsplit('.', 1)[-1])
    runtime = ROOT / 'runtime'
    cache = ROOT / 'client-data.local'
    source = cache / product / version
    profiles = [catalog for catalog in available_catalogs()
                if catalog.profile and catalog.profile.get('product') == product]
    previous = [catalog for catalog in profiles if catalog.profile['build'] < number]
    current = next((catalog for catalog in profiles if catalog.profile['client_version'] == version), None)
    print(f'Preparing maps for WoW: Forever {version}.', flush=True)
    if current is None:
        dll = runtime / 'CascLib.dll'
        extracted = extract(client, product, dll, cache, [], None, required_map_tables_only=True)
        found = {record['path'] for record in extracted['files']}
        missing = [name for name in MAP_TABLES if f'raw/dbfilesclient/{name.lower()}.db2' not in found]
        if missing:
            raise ValueError('The local client is missing required map tables: ' + ', '.join(missing))
        subprocess.run([str(runtime / 'Db2Export.exe'), str(source / 'raw/dbfilesclient'),
                        str(runtime / 'definitions'), version, str(source / 'tables'), *MAP_TABLES],
                       check=True)
        build(source, client, product, dll, None, None)
        current = next((catalog for catalog in available_catalogs()
                        if catalog.profile and catalog.profile['profile_id'] == f'{product}-{version}'), None)
        if current is None:
            raise ValueError('The generated map profile did not pass validation.')
    if previous:
        migrate(current.root, previous_profile=previous[-1].root)
    remember_catalog(current)
    print(f'{len(current.profile["zones"])} zone and city maps ready.', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('client', type=Path)
    args = parser.parse_args()
    try:
        setup(args.client)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        parser.exit(1, f'Route Me setup: {error}\n')
