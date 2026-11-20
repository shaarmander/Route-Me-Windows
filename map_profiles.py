"""Discover completed local map profiles and remember the user's map selection."""
import json
from pathlib import Path

from atlas import Atlas
from zone_maps import MapCatalog

ROOT = Path(__file__).resolve().parent
PREFERENCE = ROOT / 'map-profile.local.json'


def identity(catalog):
    return catalog.profile['profile_id'] if catalog.profile else 'classic-1.12'


def label(catalog):
    if not catalog.profile:
        return '1.12 Classic'
    version = catalog.profile['client_version']
    if catalog.profile.get('product') == 'wowf':
        return f'WoW: Forever {version}'
    return f'{version} maps'


def available_catalogs(root=None, current=None):
    root = Path(root or ROOT)
    legacy = MapCatalog(root / 'assets/maps')
    selected = {}
    if all((legacy.root / zone['file']).is_file() for zone in legacy.zones):
        selected[identity(legacy)] = legacy
    for path in sorted((root / 'client-data.local').glob('*/*/map-profile*/profile.json')):
        try:
            catalog = MapCatalog(path.parent)
            profile = catalog.profile
            if not profile or not profile['zones']:
                continue
            if not isinstance(profile['profile_id'], str) or not profile['profile_id'] or type(profile['build']) is not int or not isinstance(profile['client_version'], str):
                continue
            Atlas(catalog)  # Reject incomplete catalogs before offering them.
            for zone in profile['zones']:
                catalog.image(int(zone))
            key = identity(catalog)
            previous = selected.get(key)
            if previous is None or len(profile['zones']) > len(previous.profile['zones']):
                selected[key] = catalog
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            continue
    # An explicit CLI profile is also selectable, even outside the cache.
    if current is not None:
        selected[identity(current)] = current
    return sorted(selected.values(), key=lambda c: (bool(c.profile), c.profile.get('build', 0) if c.profile else 0))


def startup_catalog(root=None, preference=None):
    catalogs = available_catalogs(root)
    if not catalogs:
        raise ValueError('No maps are ready. Run Setup Route Me.cmd to import maps from WoW: Forever.')
    try:
        choice = json.loads(Path(preference or PREFERENCE).read_text(encoding='utf-8'))['profile_id']
        remembered = next((c for c in catalogs if identity(c) == choice), None)
        if remembered is not None:
            return remembered
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return catalogs[-1]


def remember_catalog(catalog, preference=None):
    path = Path(preference or PREFERENCE)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps({'profile_id': identity(catalog)}, indent=2) + '\n', encoding='utf-8')
    temporary.replace(path)
