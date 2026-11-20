"""Compose a client map profile from decoded DB2s and local CASC artwork.

Zone identity and bounds come from current UiMapAssignment records. Artwork is
selected by UiMapXMapArt, not legacy filename guesses. Output is staged separately
and cannot authorize collision movement until matching navigation is validated.
"""
import argparse
import hashlib
import io
import json
import math
import shutil
from pathlib import Path

from PIL import Image

from tools.extract_client_assets import DEFAULT_CLIENT, DEFAULT_PRODUCT, LocalCasc, installed_build

ROOT = Path(__file__).resolve().parents[1]


def assignment_bounds(row):
    if row['UiMin'] != [0, 0] or row['UiMax'] != [1, 1] or row['WMOGroupID'] or row['WMODoodadPlacementID']:
        raise ValueError('Only full outdoor map assignments are supported.')
    xmin, ymin, _, xmax, ymax, _ = row['Region']
    bounds = [ymax, ymin, xmax, xmin]
    if not all(math.isfinite(x) for x in bounds) or ymax <= ymin or xmax <= xmin:
        raise ValueError('Invalid map assignment bounds.')
    return bounds


def compose_art(art_id, tables, read):
    art = next(row for row in tables['UiMapArt'] if row['ID'] == art_id)
    layers = [row for row in tables['UiMapArtStyleLayer']
              if row['UiMapArtStyleID'] == art['UiMapArtStyleID'] and row['LayerIndex'] == 0]
    if len(layers) != 1:
        raise ValueError('Expected one base map layer.')
    layer = layers[0]
    width, height = layer['LayerWidth'], layer['LayerHeight']
    tw, th = layer['TileWidth'], layer['TileHeight']
    if not 0 < width <= 8192 or not 0 < height <= 8192 or tw <= 0 or th <= 0:
        raise ValueError('Unsupported map dimensions.')
    tiles = [row for row in tables['UiMapArtTile'] if row['UiMapArtID'] == art_id and row['LayerIndex'] == 0]
    expected = {(x, y) for y in range(math.ceil(height / th)) for x in range(math.ceil(width / tw))}
    found = {(row['ColIndex'], row['RowIndex']) for row in tiles}
    if found != expected or len(tiles) != len(expected):
        raise ValueError('Incomplete or duplicate map artwork tiles.')
    image = Image.new('RGBA', (width, height))
    for row in tiles:
        with Image.open(io.BytesIO(read(row['FileDataID']))) as tile:
            if tile.size != (tw, th):
                raise ValueError('Map tile dimensions disagree with style metadata.')
            image.paste(tile.convert('RGBA'), (row['ColIndex'] * tw, row['RowIndex'] * th))
    for overlay in tables['WorldMapOverlay']:
        if overlay['UiMapArtID'] != art_id:
            continue
        ow, oh = overlay['TextureWidth'], overlay['TextureHeight']
        all_records = [r for r in tables['WorldMapOverlayTile'] if r['WorldMapOverlayID'] == overlay['ID']]
        # Stranglethorn overlay 5252 has a hit rectangle and placeholder
        # dimensions, but the Forever archive has no tile records for it.
        if not all_records and ((ow == 0 and oh == 0) or (art_id, overlay['ID']) == (2160, 5252)):
            continue
        if overlay['PlayerConditionID']:
            raise ValueError('Conditional map overlays need live phase selection.')
        if not 0 < ow <= 8192 or not 0 < oh <= 8192:
            raise ValueError('Invalid overlay dimensions.')
        records = [r for r in all_records if r['LayerIndex'] == 0]
        expected = {(x, y) for y in range(math.ceil(oh / 256)) for x in range(math.ceil(ow / 256))}
        if {(r['ColIndex'], r['RowIndex']) for r in records} != expected or len(records) != len(expected):
            raise ValueError('Incomplete map overlay tiles.')
        for row in records:
            x, y = row['ColIndex'] * 256, row['RowIndex'] * 256
            needed = (min(256, ow - x), min(256, oh - y))
            with Image.open(io.BytesIO(read(row['FileDataID']))) as tile:
                if tile.width < needed[0] or tile.height < needed[1]:
                    raise ValueError('Overlay tile is too small.')
                image.alpha_composite(tile.convert('RGBA').crop((0, 0, *needed)),
                                      (overlay['OffsetX'] + x, overlay['OffsetY'] + y))
    return image.convert('RGB'), art


def unphased_art(map_id, tables):
    arts = [row['UiMapArtID'] for row in tables['UiMapXMapArt']
            if row['UiMapID'] == map_id and row['PhaseID'] == 0]
    if len(arts) != 1:
        raise ValueError(f'Expected one unphased base art for UI map {map_id}.')
    return arts[0]


def build(source, client, product, dll, keys, area_ids, output=None):
    """Use current overworld maps and overviews when no areas are specified."""
    manifest = json.loads((source / 'manifest.json').read_text())
    if (manifest['product'], manifest['client_version']) != (product, installed_build(client, product)):
        raise ValueError('Extracted source build differs from installed client.')
    tables = {name: json.loads((source / 'tables' / (name + '.json')).read_text()) for name in
              ('UiMap', 'UiMapAssignment', 'UiMapArt', 'UiMapArtStyleLayer', 'UiMapArtTile', 'UiMapXMapArt', 'WorldMapOverlay', 'WorldMapOverlayTile')}
    base = ROOT / 'assets/maps'
    output = output or source / 'map-profile'
    if (output/'profile.json').exists():
        raise ValueError('A completed profile already exists. Choose a new --output directory to preserve its map and road edits.')
    output.mkdir(parents=True,exist_ok=True)
    catalog = json.loads((base / 'catalog.json').read_text())
    atlas = json.loads((base / 'atlas.json').read_text())
    maps = {row['ID']: row for row in tables['UiMap'] if row['System'] == 0}
    assignments = [row for row in tables['UiMapAssignment'] if row['UiMapID'] in maps]
    for path in base.rglob('*'):
        if path.is_file():
            dest = output / path.relative_to(base)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, dest)
    metadata = {'version': 1, 'profile_id': product + '-' + manifest['client_version'],
                'product': product, 'client_version': manifest['client_version'],
                'build': int(manifest['client_version'].split('.')[-1]),
                'zones': {}, 'legacy_bounds': {k: v['bounds'] for k, v in atlas['zones'].items()},
                'navigation_ready': False, 'navigation_status': 'matching-client-mesh-not-generated'}
    metadata['raw_database_sha256']={record['path']:record['sha256'] for record in manifest['files'] if record['path'].endswith('.db2')}
    for name in ('dbcd-source','dbd-source'):
        path=ROOT/'native/build/client-extraction'/f'{name}.json'
        if path.exists():metadata[name]=json.loads(path.read_text())
    store = LocalCasc(dll, client, product, keys)
    raw_art = source / 'raw/active-map-art'
    raw_art.mkdir(exist_ok=True)
    files = {}

    def read(fid):
        data = store.read(fid)
        (raw_art / f'{fid}.blp').write_bytes(data)
        files[str(fid)] = hashlib.sha256(data).hexdigest()
        return data

    def render_map(map_id, filename):
        art_id = unphased_art(map_id, tables)
        image, art = compose_art(art_id, tables, read)
        image.save(output / filename, quality=96)
        return image, art

    try:
        all_overworld = area_ids is None
        if all_overworld:
            zone_assignments = [row for row in assignments
                                if row['AreaID'] and maps[row['UiMapID']]['Type'] == 3]
            area_ids = sorted({row['AreaID'] for row in zone_assignments})
            missing = {zone['id'] for zone in catalog} - set(area_ids)
            if missing:
                raise ValueError(f'Current overworld assignments missing for areas {sorted(missing)}.')
        else:
            zone_assignments = assignments
        for area in area_ids:
            candidates = [row for row in zone_assignments if row['AreaID'] == area]
            if len(candidates) != 1:
                raise ValueError(f'Expected one current map assignment for area {area}.')
            assignment = candidates[0]
            map_id, map_info = assignment['UiMapID'], maps[assignment['UiMapID']]
            continent = assignment['MapID']
            zone = next((zone for zone in catalog if zone['id'] == area), None)
            if zone is None and all_overworld:
                zone = {'id': area, 'name': map_info['Name_lang'], 'file': f'{area}.jpg'}
                catalog.append(zone)
            if zone is None:
                raise ValueError(f'No bundled map for area {area}.')
            bounds = assignment_bounds(assignment)
            image, art = render_map(map_id, zone['file'])
            entry = atlas['zones'].setdefault(str(area), {'id': area})
            entry.update(bounds=bounds, continent=continent)
            entry.pop('highlight', None)
            highlight = art['HighlightFileDataID']
            if highlight:
                filename = f'highlights/{area}.png'
                destination = output / filename
                destination.parent.mkdir(exist_ok=True)
                with Image.open(io.BytesIO(read(highlight))) as mask:
                    mask.convert('RGB').save(destination)
                entry['highlight'] = filename
            if all_overworld and str(continent) not in atlas['continents']:
                # An outdoor zone on a separate game map has its own overview.
                atlas['continents'][str(continent)] = dict(
                    continent=continent, name=zone['name'], file=zone['file'],
                    zone_id=area, bounds=bounds)
            metadata['zones'][str(area)] = {'ui_map_id': map_id, 'art_id': art['ID'],
                'bounds': bounds, 'continent': continent, 'width': image.width,
                'height': image.height, 'navigation_ready': False}
            print('Built active client map:', zone['name'], image.size, bounds, flush=True)
        if all_overworld:
            world, = [row for row in maps.values() if row['Type'] == 1 and not row['ParentUiMapID']]
            render_map(world['ID'], 'World.jpg')
            atlas['world_continents'] = {}
            for row in assignments:
                info = maps[row['UiMapID']]
                if info['ID'] == world['ID']:
                    atlas['world_continents'][str(row['MapID'])] = [*row['UiMin'], *row['UiMax']]
                if info['Type'] != 2 or info['ParentUiMapID'] != world['ID']:
                    continue
                record = atlas['continents'][str(row['MapID'])]
                record['bounds'] = assignment_bounds(row)
                render_map(info['ID'], record['file'])
        metadata['art_sha256'] = files
        (output / 'catalog.json').write_text(json.dumps(catalog, indent=2), encoding='utf-8')
        (output / 'atlas.json').write_text(json.dumps(atlas, indent=2), encoding='utf-8')
        (output / 'profile.json').write_text(json.dumps(metadata, indent=2), encoding='utf-8')
        print('Staged profile:', output)
        return output
    finally:
        store.close()


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source', type=Path)
    p.add_argument('--client', type=Path, default=DEFAULT_CLIENT)
    p.add_argument('--product', default=DEFAULT_PRODUCT)
    p.add_argument('--dll', type=Path, default=ROOT / 'native/build/client-extraction/casc/Release/CascLib.dll')
    p.add_argument('--keys', type=Path, default=ROOT / 'native/build/client-extraction/WoW-TACTKeys.txt')
    p.add_argument('--areas', nargs='+', type=int, help='Limit the profile to specific area IDs. Defaults to all overworld maps.')
    p.add_argument('--output',type=Path,help='Use a new destination when rebuilding a completed profile.')
    a = p.parse_args()
    source = a.source or ROOT / 'client-data.local' / a.product / installed_build(a.client, a.product)
    build(source, a.client, a.product, a.dll, a.keys, a.areas, a.output)
