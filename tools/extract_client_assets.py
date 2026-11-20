"""Read map assets from a selected installed CASC client into an isolated cache.

This exports source data, not app-ready navigation tiles. It never modifies the
game installation or replaces the app's atlas. Requires a 64-bit ANSI CascLib DLL.
"""
import argparse
import ctypes as C
import hashlib
import io
import json
import struct
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CLIENT = Path(r'C:\Program Files (x86)\World of Warcraft')
DEFAULT_PRODUCT = 'wowf'
# wowdev/wow-listfile release 202609292337. IDs are checked against this product's
# root by CascLib; filenames alone cannot resolve many modern client files.
MAP_FILES = {
    'dbfilesclient/map.db2': 1349477,
    'dbfilesclient/uimap.db2': 1957206,
    'dbfilesclient/uimapassignment.db2': 1957219,
    'dbfilesclient/uimapart.db2': 1957202,
    'dbfilesclient/uimapartstylelayer.db2': 1957208,
    'dbfilesclient/uimaparttile.db2': 1957210,
    'dbfilesclient/uimapxmapart.db2': 1957217,
    'dbfilesclient/worldmapoverlay.db2': 1134579,
    'dbfilesclient/worldmapoverlaytile.db2': 1957212,
    'world/maps/azeroth/azeroth.wdt': 775971,
    'world/maps/kalimdor/kalimdor.wdt': 782779,
}


def installed_build(client, product):
    lines = (client / '.build.info').read_text(encoding='utf-8-sig').splitlines()
    keys = [column.split('!', 1)[0] for column in lines[0].split('|')]
    matches = [dict(zip(keys, line.split('|'))) for line in lines[1:] if line]
    matches = [row for row in matches if row.get('Product') == product and row.get('Active') == '1']
    if len(matches) != 1:
        raise ValueError(f'Expected one active build for {product}, found {len(matches)}.')
    return matches[0]['Version']


class LocalCasc:
    def __init__(self, dll, client, product, keys=None):
        if not hasattr(C, 'WinDLL'):
            raise RuntimeError('Local CASC extraction currently requires Windows.')
        self.dll = C.WinDLL(str(dll.resolve()), use_last_error=True)
        signatures = {
            'CascOpenStorage': ([C.c_char_p, C.c_uint32, C.POINTER(C.c_void_p)], C.c_bool),
            'CascOpenFile': ([C.c_void_p, C.c_void_p, C.c_uint32, C.c_uint32, C.POINTER(C.c_void_p)], C.c_bool),
            'CascGetFileSize': ([C.c_void_p, C.POINTER(C.c_uint32)], C.c_uint32),
            'CascReadFile': ([C.c_void_p, C.c_void_p, C.c_uint32, C.POINTER(C.c_uint32)], C.c_bool),
            'CascCloseFile': ([C.c_void_p], C.c_bool),
            'CascCloseStorage': ([C.c_void_p], C.c_bool),
            'CascAddStringEncryptionKey': ([C.c_void_p, C.c_uint64, C.c_char_p], C.c_bool),
        }
        for name, (args, result) in signatures.items():
            fn = getattr(self.dll, name)
            fn.argtypes, fn.restype = args, result
        self.handle = C.c_void_p()
        # The explicit product avoids accidentally extracting Retail from shared Data.
        params = (str(client.resolve()) + '*' + product).encode('mbcs')
        if not self.dll.CascOpenStorage(params, 2, C.byref(self.handle)):
            raise OSError(C.get_last_error(), f'Cannot open local {product} archive.')
        if keys is not None:
            try:
                for line in keys.read_text(encoding='utf-8').splitlines():
                    if not line.strip() or line.lstrip().startswith('#'):
                        continue
                    name, key = line.split()
                    if len(name) != 16 or len(key) != 32:
                        raise ValueError('Invalid TACT key record.')
                    int(key, 16)
                    if not self.dll.CascAddStringEncryptionKey(self.handle, int(name, 16), key.encode('ascii')):
                        raise OSError(C.get_last_error(), 'Cannot load a supplied TACT key.')
            except Exception:
                self.close()
                raise

    def read(self, identifier, allow_missing_keys=False):
        handle = C.c_void_p()
        name = identifier.encode('utf-8') if isinstance(identifier, str) else identifier
        flags = 0 if isinstance(identifier, str) else 3  # CASC_OPEN_BY_FILEID
        # Diagnostic use only: explicitly mark any resulting file as partial.
        # Normal extraction never substitutes zeros for unavailable content.
        if allow_missing_keys:
            flags |= 0x20
        if not self.dll.CascOpenFile(self.handle, name, 2, flags, C.byref(handle)):
            raise OSError(C.get_last_error(), f'Cannot open {identifier!r}.')
        try:
            high = C.c_uint32()
            size = self.dll.CascGetFileSize(handle, C.byref(high))
            if high.value or size == 0xFFFFFFFF or size > 128 * 1024 * 1024:
                raise ValueError(f'Unexpected map asset size: {identifier!r}.')
            buffer, offset = C.create_string_buffer(size), 0
            while offset < size:
                count = C.c_uint32()
                if not self.dll.CascReadFile(handle, C.byref(buffer, offset), size - offset, C.byref(count)):
                    raise OSError(C.get_last_error(), f'Cannot read {identifier!r}.')
                if not count.value:
                    raise ValueError(f'Truncated asset: {identifier!r} ({offset}/{size} bytes).')
                offset += count.value
            return buffer.raw
        finally:
            self.dll.CascCloseFile(handle)

    def close(self):
        self.dll.CascCloseStorage(self.handle)


def chunks(data):
    offset = 0
    while offset < len(data):
        if offset + 8 > len(data):
            raise ValueError('Truncated chunk header.')
        tag, size = struct.unpack_from('<4sI', data, offset)
        end = offset + 8 + size
        if end > len(data):
            raise ValueError('Truncated chunk body.')
        yield tag[::-1].decode('ascii'), data[offset + 8:end]
        offset = end


def terrain_ids(wdt, bounds):
    """Select terrain near a legacy zone rectangle, without claiming new bounds.

    WDT MAID entries are ordered row (world X) then column (world Y). Keep
    additional MAI2 data in the raw WDT; this helper exports baseline MAID only.
    """
    import math
    left, right, top, bottom = bounds
    columns = [math.floor(32 - p / (1600 / 3)) for p in (left, right)]
    rows = [math.floor(32 - p / (1600 / 3)) for p in (top, bottom)]
    table = dict(chunks(wdt)).get('MAID')
    if table is None or len(table) != 64 * 64 * 32:
        raise ValueError('Expected a 64 by 64 WDT MAID table.')
    result = []
    for row in range(max(0, min(rows) - 1), min(63, max(rows) + 1) + 1):
        for col in range(max(0, min(columns) - 1), min(63, max(columns) + 1) + 1):
            ids = struct.unpack_from('<8I', table, (row * 64 + col) * 32)
            if ids[0]:
                result.append({'row': row, 'column': col, 'file_ids': dict(zip(
                    ('root_adt', 'objects_adt', 'objects1_adt', 'textures_adt',
                     'lod_adt', 'map_texture', 'map_normal', 'minimap_texture'), ids))})
    return result


def extract(client, product, dll, output, textures, terrain_zone, extra_file_list=None, keys=None,
            *, required_map_tables_only=False):
    version = installed_build(client, product)
    output = output / product / version
    output.mkdir(parents=True, exist_ok=True)
    manifest = {
        'version': 1, 'product': product, 'client_version': version,
        'extracted_utc': datetime.now(timezone.utc).isoformat(),
        'status': 'source-assets-only',
        'notes': [
            'Map image previews use named legacy 4x3 tile sets present in this build.',
            'Active artwork, dimensions and bounds must be resolved through the exported UiMap tables or addon APIs.',
            'Raw terrain is not a generated navigation mesh; model dependencies are not yet exported.',
            'MAI2 terrain variants are preserved in WDT files, not interpreted by this exporter.',
            'No working app assets or game files were replaced.',
        ],
        'sources': {
            'reader': 'https://github.com/ladislav-zezula/CascLib/tree/3.0',
            'file_ids': 'https://github.com/wowdev/wow-listfile/releases/tag/202609292337',
        },
        'files': [], 'map_previews': [], 'errors': [], 'absent_candidates': [],
    }
    if keys is not None:
        manifest['supplied_key_file_sha256'] = hashlib.sha256(keys.read_bytes()).hexdigest()
    store = LocalCasc(dll, client, product, keys)

    def save(identifier, relative):
        data = store.read(identifier)
        path = output / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        manifest['files'].append({'path': relative, 'archive_identifier': identifier,
                                  'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()})
        return data

    try:
        tables = {}
        for name, fid in MAP_FILES.items():
            if required_map_tables_only and (not name.endswith('.db2') or name == 'dbfilesclient/map.db2'):
                continue
            try:
                tables[name] = save(fid, 'raw/' + name)
            except (OSError, ValueError) as error:
                manifest['errors'].append({'asset': name, 'error': str(error)})
        for texture in textures:
            if not texture.isalnum():
                raise ValueError('Map texture names must be alphanumeric.')
            try:
                image = Image.new('RGB', (1024, 768))
                for i in range(12):
                    name = f'interface/worldmap/{texture.lower()}/{texture.lower()}{i + 1}.blp'
                    data = save(name, 'raw/' + name)
                    with Image.open(io.BytesIO(data)) as tile:
                        if tile.size != (256, 256):
                            raise ValueError(f'Unexpected legacy tile size {tile.size}.')
                        image.paste(tile.convert('RGB'), (i % 4 * 256, i // 4 * 256))
                relative = f'previews/{texture}.png'
                path = output / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                # Retain the full texture mosaic; do not guess current display bounds.
                image.save(path)
                manifest['map_previews'].append({'texture': texture, 'path': relative,
                                                'active_art_verified': False})
                print('Extracted map preview:', texture, flush=True)
            except (OSError, ValueError) as error:
                manifest['errors'].append({'asset': texture, 'error': str(error)})
        if extra_file_list is not None:
            for line in extra_file_list.read_text(encoding='utf-8').splitlines():
                fid, name = line.split(';', 1)
                # Only candidate map overlays from a shared cross-build listfile.
                if not name.startswith('interface/worldmap/') or not name.endswith('.blp'):
                    continue
                if any(part in ('..', '.') for part in name.split('/')) or '\\' in name:
                    raise ValueError('Invalid overlay asset path.')
                try:
                    save(int(fid), 'raw/' + name)
                except OSError as error:
                    if error.errno == 2:
                        manifest['absent_candidates'].append({'file_id': int(fid), 'name': name})
                    else:
                        manifest['errors'].append({'asset': int(fid), 'error': str(error)})
        if terrain_zone is not None:
            atlas = json.loads((ROOT / 'assets/maps/atlas.json').read_text())
            zone = atlas['zones'][str(terrain_zone)]
            name = 'azeroth' if zone['continent'] == 0 else 'kalimdor'
            wdt = tables.get(f'world/maps/{name}/{name}.wdt')
            if wdt is None:
                raise ValueError('Missing terrain index for selected zone.')
            entries = terrain_ids(wdt, zone['bounds'])
            manifest['terrain_selection'] = {'legacy_area_id': terrain_zone,
                'selection_bounds_source': 'bundled-1.12-atlas-with-one-tile-padding',
                'entries': entries}
            for entry in entries:
                for kind in ('root_adt', 'objects_adt', 'objects1_adt', 'textures_adt'):
                    fid = entry['file_ids'][kind]
                    if fid:
                        try:
                            save(fid, f'raw/terrain/{fid}.adt')
                        except (OSError, ValueError) as error:
                            manifest['errors'].append({'asset': fid, 'error': str(error)})
            print('Selected terrain tiles:', len(entries), flush=True)
    finally:
        store.close()
        (output / 'manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    print(f"Saved {len(manifest['files'])} source files; {len(manifest['errors'])} extraction errors.")
    print(output)
    return manifest


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--client', type=Path, default=DEFAULT_CLIENT)
    parser.add_argument('--product', default=DEFAULT_PRODUCT)
    parser.add_argument('--dll', type=Path, default=ROOT / 'native/build/client-extraction/casc/Release/CascLib.dll')
    parser.add_argument('--output', type=Path, default=ROOT / 'client-data.local')
    parser.add_argument('--textures', nargs='+', default=['Redridge', 'Azeroth', 'Kalimdor', 'World'])
    parser.add_argument('--terrain-zone', type=int, default=44)
    parser.add_argument('--maps-only', action='store_true', help='Skip legacy texture previews and terrain samples.')
    parser.add_argument('--extra-file-list', type=Path, help='Optional FileDataID;filename CSV of map-overlay candidates.')
    parser.add_argument('--keys', type=Path, help='Optional public TACT key list (hex name and key per line).')
    args = parser.parse_args()
    extract(args.client, args.product, args.dll, args.output,
            [] if args.maps_only else args.textures, None if args.maps_only else args.terrain_zone,
            args.extra_file_list, args.keys)
