"""Project saved world roads and desktop overlay clicks onto client maps."""
import json
import math


from navigation import NavigationError, ZoneTransform
from road_curves import flatten
from tools.build_client_map_profile import assignment_bounds


def client_maps(atlas):
    profile = atlas.catalog.profile
    if not profile:
        raise ValueError('Select an extracted client map profile before using the desktop overlay.')
    source = atlas.catalog.root.parent
    manifest = json.loads((source/'manifest.json').read_text(encoding='utf-8'))
    if (manifest['client_version'], manifest['product']) != (profile['client_version'], profile['product']):
        raise ValueError('Map assignments do not match the selected client profile.')
    maps = {row['ID']: row for row in json.loads((source/'tables/UiMap.json').read_text())}
    assignments = json.loads((source/'tables/UiMapAssignment.json').read_text())
    result = {}
    for row in assignments:
        info = maps[row['UiMapID']]
        area, continent = row['AreaID'], row['MapID']
        if info['System'] != 0 or continent not in atlas.continents:
            continue
        if area not in atlas.zones and not (area == 0 and info['Type'] == 2):
            continue
        try:
            bounds = assignment_bounds(row)
        except ValueError:
            continue
        if area:
            zone = atlas.zones[area]
            if zone['continent'] != continent or max(abs(a-b) for a,b in zip(zone['bounds'],bounds)) > 1.:
                continue  # Updated geometry requires a corresponding extracted map profile.
        if row['UiMapID'] in result:
            raise ValueError('Ambiguous live map assignment.')
        result[row['UiMapID']] = dict(zone=area, continent=continent, bounds=bounds)
    return result


def clip_segment(a, b):
    """Clip one drawing segment to the native map rectangle, without moving roads."""
    dx, dy = b[0]-a[0], b[1]-a[1]
    lo, hi = 0., 1.
    for p, q in ((-dx,a[0]),(dx,1-a[0]),(-dy,a[1]),(dy,1-a[1])):
        if abs(p) < 1e-12:
            if q < 0: return None
        elif p < 0:
            lo = max(lo,q/p)
        else:
            hi = min(hi,q/p)
        if lo > hi: return None
    if hi-lo < 1e-12: return None
    return [max(0.,min(1.,v)) for v in
            (a[0]+lo*dx,a[1]+lo*dy,a[0]+hi*dx,a[1]+hi*dy)]


def road_data(atlas, store, maps=None):
    maps = client_maps(atlas) if maps is None else maps
    world_roads = {}
    for zone in atlas.zones:
        roads, curves = store.load_document(zone)
        if not roads: continue
        t = atlas.transform(zone)
        world_roads[zone] = [[t.world((x*t.width,y*t.height),0.) for x,y in
                              (flatten(curve,t.width,t.height) if curve is not None else road)]
                             for road,curve in zip(roads,curves)]
    output = {}
    for map_id, record in maps.items():
        t = ZoneTransform(record['zone'],record['continent'],*record['bounds'],1,1)
        segments = []
        for zone,roads in world_roads.items():
            if (record['zone'] and zone != record['zone']) or atlas.zones[zone]['continent'] != record['continent']:
                continue
            for road in roads:
                points = [t.pixel(p) for p in road]
                for a,b in zip(points,points[1:]):
                    segment = clip_segment(a,b)
                    if segment is not None: segments.append(segment)
        if segments:
            if len(segments) > 20000: raise ValueError('Too many road segments for the desktop overlay.')
            output[map_id] = dict(zone=record['zone'],segments=segments)
    return dict(version=1,build=atlas.catalog.profile['build'],maps=output)



def map_destination(atlas, maps, packet, map_id, x, y):
    """Convert an external overlay click using the displayed map, not the player zone."""
    atlas.packet_zone(packet)
    record = maps.get(map_id)
    if record is None: raise NavigationError('This displayed map is not supported for road destinations.')
    if record['continent'] != packet['continent']:
        raise NavigationError('Choose a destination on your current continent.')
    if not all(math.isfinite(v) and 0 <= v <= 1 for v in (x,y)):
        raise NavigationError('Invalid map click coordinates.')
    t = ZoneTransform(record['zone'],record['continent'],*record['bounds'],1,1)
    world = t.world((x,y),0.)
    zone = record['zone'] or atlas.zone_at(record['continent'],world)
    if zone is None: raise NavigationError('Choose a destination inside a supported zone.')
    atlas.catalog.require_navigation(zone)
    return dict(continent=record['continent'],world=world,zone=zone)
