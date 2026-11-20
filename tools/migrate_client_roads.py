"""Copy legacy roads into a client profile, preserving world-space curves.

Original road files are never changed, and existing profile edits are not replaced.
"""
import argparse
import hashlib
import json
import shutil
from pathlib import Path

from atlas import Atlas
from road_network import RoadStore
from zone_maps import MapCatalog

ROOT=Path(__file__).resolve().parents[1]


def migrate(profile_root, source_root=None, previous_profile=None):
    legacy=Atlas(MapCatalog(ROOT/'assets/maps'))
    current=Atlas(MapCatalog(profile_root))
    if not current.catalog.profile:
        raise ValueError('Select a client map profile.')
    source=RoadStore(source_root or ROOT/'roads.local')
    destination=RoadStore(profile_root/'roads-world',current.transform,legacy.transform,
                          current.catalog.profile['profile_id'])
    records=[]
    if previous_profile is not None:
        prior=RoadStore(previous_profile/'roads-world',current.transform)
        for path in sorted(prior.root.glob('*.json')):
            zone_id=int(path.stem)
            if destination.path(zone_id).exists():
                continue
            original=path.read_bytes()
            data=json.loads(original)
            if data.get('version')!=3 or data.get('coordinates')!='world':
                raise ValueError('Existing trial roads must use world coordinates before copying.')
            roads,_=prior.load_document(zone_id) # Check reprojection against the new maps.
            destination.root.mkdir(parents=True,exist_ok=True)
            shutil.copy2(path,destination.path(zone_id))
            if path.read_bytes()!=original or destination.path(zone_id).read_bytes()!=original:
                raise ValueError('Existing trial road file changed while copying; retry without replacing edits.')
            records.append({'zone_id':zone_id,'source_sha256':hashlib.sha256(original).hexdigest(),
                            'source':str(path),'kind':'existing-world-road-copy','strokes':len(roads),
                            'destination':str(destination.path(zone_id))})
            print('Retained existing trial road edits:',zone_id,len(roads),'strokes')
    for path in sorted(source.root.glob('*.json')):
        zone_id=int(path.stem)
        if zone_id not in current.zones:
            raise ValueError(f'No map transform for saved roads in zone {zone_id}.')
        if destination.path(zone_id).exists():
            print('Retained existing profile roads:',zone_id)
            continue
        original=path.read_bytes()
        roads,curves=source.load_document(zone_id)
        destination.import_legacy(zone_id,roads,curves)
        assert path.read_bytes()==original
        records.append({'zone_id':zone_id,'source_sha256':hashlib.sha256(original).hexdigest(),
                        'strokes':len(roads),'destination':str(destination.path(zone_id))})
        print('Copied roads in world coordinates:',zone_id,len(roads),'strokes')
    if records:
        manifest_path=profile_root/'road-migration.json'
        previous=json.loads(manifest_path.read_text()) if manifest_path.exists() else []
        manifest_path.write_text(json.dumps(previous+records,indent=2),encoding='utf-8')
    return records


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('profile',type=Path)
    p.add_argument('--roads',type=Path)
    p.add_argument('--previous-profile',type=Path,help='Carry over world-coordinate road edits from an earlier trial.')
    args=p.parse_args()
    migrate(args.profile,args.roads,args.previous_profile)
