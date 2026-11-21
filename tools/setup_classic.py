"""Prepare a local 1.12.1 map catalog and collision data from the user's client."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
from urllib.request import urlopen
from zipfile import BadZipFile, ZipFile

from map_profiles import LOCAL_CLASSIC, ROOT, classic_catalog, remember_catalog
from navigation import Detour, NAV_DLL, Tile
from tools.build_atlas import build

EXTRACTORS_URL = (
    'https://github.com/cmangos/mangos-classic/releases/download/latest/'
    'mangos-classic-with-all-28a26c38.zip'
)
EXTRACTORS_SHA256 = '4eae50782cb8042d28222de431075332db6fd8eedf16a27bb5a7c373a446d6a7'
EXTRACTORS_FILES = (
    'ad.exe', 'vmap_extractor.exe', 'vmap_assembler.exe', 'MoveMapGen.exe',
    'config.json', 'offmesh.txt',
)


def extraction_tools(cache):
    archive_path = cache / 'cmangos-classic-28a26c38.zip'
    if not archive_path.is_file():
        print('Downloading CMaNGOS Classic extraction tools from GitHub…', flush=True)
        temporary = archive_path.with_suffix('.download')
        with urlopen(EXTRACTORS_URL, timeout=90) as response, temporary.open('wb') as output:
            shutil.copyfileobj(response, output)
        temporary.replace(archive_path)
    with archive_path.open('rb') as downloaded:
        if hashlib.file_digest(downloaded, 'sha256').hexdigest() != EXTRACTORS_SHA256:
            raise ValueError(f'CMaNGOS download checksum differs. Remove {archive_path} and retry.')
    tools = cache / 'cmangos-classic-28a26c38'
    tools.mkdir(exist_ok=True)
    with ZipFile(archive_path) as archive:
        for name in EXTRACTORS_FILES:
            member = f'x64_RelWithDebInfo/Extractors/{name}'
            with archive.open(member) as source, (tools / name).open('wb') as output:
                shutil.copyfileobj(source, output)
    return tools


def generate_meshes(client):
    cache = ROOT / 'client-data.local'
    cache.mkdir(exist_ok=True)
    tools = extraction_tools(cache)
    output = Path(tempfile.mkdtemp(prefix='classic-extraction-', dir=cache))
    print(f'Generating Classic data in {output}. Mesh generation can take hours.', flush=True)

    def run(tool, *arguments):
        log = output / f'{tool}.log'
        print(f'Running {tool}. Progress log: {log}', flush=True)
        with log.open('wb') as stream:
            result = subprocess.run([str(tools / f'{tool}.exe'), *map(str, arguments)],
                                    cwd=output, stdout=stream, stderr=subprocess.STDOUT)
        if result.returncode:
            raise ValueError(f'{tool} stopped with exit code {result.returncode}. See {log}.')

    run('ad', '-i', client, '-o', output)
    run('vmap_extractor', '-d', client / 'Data', '-o', output)
    (output / 'vmaps').mkdir(exist_ok=True)
    run('vmap_assembler', output / 'Buildings', output / 'vmaps')
    (output / 'mmaps').mkdir(exist_ok=True)
    run('MoveMapGen', '0 1', '--workdir', output, '--silent',
        '--configInputPath', tools / 'config.json', '--offMeshInput', tools / 'offmesh.txt')
    return output


def check_data(root):
    for name in ('WorldMapArea.dbc', 'WorldMapOverlay.dbc'):
        if not (root / 'dbc' / name).is_file():
            raise ValueError(f'Missing dbc/{name} in {root}.')
    continents = []
    for continent, name in (('000', 'Eastern Kingdoms'), ('001', 'Kalimdor')):
        directory = root / 'mmaps'
        first = next(iter(sorted(directory.glob(f'{continent}*.mmtile'))), None)
        if first is None:
            continue
        params = (directory / f'{continent}.mmap').read_bytes()
        # Load a real tile through the packaged adapter as well as checking its header.
        engine = Detour(params)
        engine.add(Tile.read(first))
        continents.append(name)
    if not continents:
        raise ValueError(f'No Eastern Kingdoms or Kalimdor .mmtile files in {root / "mmaps"}.')
    return continents


def setup(client, data_root=None, force_generate=False):
    if not NAV_DLL.is_file():
        raise ValueError('Download the full Windows release ZIP for the Classic navigation adapter.')
    settings = ROOT / 'navigation.local.json'
    generated = force_generate
    if generated:
        data_root = generate_meshes(client)
    elif data_root is None:
        if settings.is_file():
            configured = json.loads(settings.read_text(encoding='utf-8')).get('data_root')
            if configured and Path(configured).is_dir():
                data_root = Path(configured)
        if data_root is None:
            generated = not (client / 'mmaps').is_dir()
            data_root = generate_meshes(client) if generated else client
    try:
        continents = check_data(data_root)
    except (OSError, ValueError) as error:
        raise ValueError(f'{error} Run Setup Classic 1.12.cmd -GenerateMeshes to regenerate the data.') from error
    if generated and len(continents) != 2:
        raise ValueError('Mesh generation did not finish both continents. Check the MoveMapGen log.')
    print('Verified navigation mesh: ' + ', '.join(continents), flush=True)
    output = ROOT / LOCAL_CLASSIC
    build(client, output, data_root=data_root, catalog_path=ROOT / 'assets/maps/catalog.json')
    temporary = settings.with_suffix('.tmp')
    temporary.write_text(json.dumps({'data_root':str(data_root)},indent=2)+'\n',encoding='utf-8')
    temporary.replace(settings)
    remember_catalog(classic_catalog())


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('client', type=Path)
    options = parser.add_mutually_exclusive_group()
    options.add_argument('--data-root', type=Path)
    options.add_argument('--generate-meshes', action='store_true')
    args = parser.parse_args()
    try:
        setup(args.client.resolve(), args.data_root.resolve() if args.data_root else None, args.generate_meshes)
    except (OSError, ValueError, BadZipFile, KeyError) as error:
        parser.exit(1, f'Classic setup: {error}\n')
