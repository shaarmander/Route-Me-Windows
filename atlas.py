"""Classic map hierarchy in shared world coordinates; no live game interaction."""
import json
import math
from pathlib import Path
import re

import numpy as np
from PIL import Image

from core import distance
from navigation import CITIES, NavigationError, ZoneTransform
from zone_maps import MapCatalog


class Atlas:
    def __init__(self, catalog=None, metadata=None):
        self.catalog=catalog or MapCatalog()
        data=metadata or json.loads((self.catalog.root/'atlas.json').read_text(encoding='utf-8'))
        self.continents={int(k):v for k,v in data['continents'].items()}
        self.world_continents={int(k):v for k,v in data.get('world_continents',{}).items()}
        self.zones={int(k):dict(v,**self.catalog.by_id[int(k)]) for k,v in data['zones'].items()
                    if int(k) in self.catalog.by_id}
        self.names={self.normal(z['name']):z['id'] for z in self.zones.values()}
        self.images={}
        self.transforms={}
        self.highlights={}

    @staticmethod
    def normal(name):
        return re.sub('[^a-z]','',name.lower()).removeprefix('the')

    def image(self, continent):
        if continent is None:
            key='World.jpg'
        else:
            record=self.continents[continent]
            key=record['file'] if 'file' in record else record['texture']+'.jpg'
        if key not in self.images:
            self.images[key]=np.asarray(Image.open(self.catalog.root/key).convert('RGB')).copy()
        return self.images[key]

    def world_continent_at(self, pixel):
        if not self.world_continents:
            return 1 if pixel[0]<501 else 0
        height,width=self.image(None).shape[:2]
        x,y=pixel[0]/width,pixel[1]/height
        return next((continent for continent,(left,top,right,bottom) in self.world_continents.items()
                     if left<=x<=right and top<=y<=bottom),None)

    def transform(self, zone_id):
        if zone_id not in self.transforms:
            zone=self.zones[zone_id];shape=self.catalog.image(zone_id).shape
            self.transforms[zone_id]=ZoneTransform(zone_id,zone['continent'],*zone['bounds'],shape[1],shape[0])
        return self.transforms[zone_id]

    def frame(self, continent):
        record=self.continents[continent];h,w=self.image(continent).shape[:2]
        return ZoneTransform(0,continent,*record['bounds'],w,h)

    def zone_at(self, continent, world):
        choices=[]
        for zone in self.zones.values():
            if zone['continent']!=continent or (zone['id'] in CITIES and self.catalog.require_navigation(zone['id'])): continue
            left,right,top,bottom=zone['bounds']
            if bottom<=world[0]<=top and right<=world[1]<=left:
                area=(left-right)*(top-bottom)
                strength=1
                if zone.get('highlight'):
                    if zone['id'] not in self.highlights:
                        self.highlights[zone['id']]=np.asarray(Image.open(self.catalog.root/zone['highlight']).convert('L'))
                    mask=self.highlights[zone['id']];h,w=mask.shape
                    t=self.transform(zone['id']);p=t.pixel(world)
                    # Highlight textures are square/power-of-two padded. Their
                    # meaningful map height follows the zone artwork's aspect.
                    x=min(w-1,max(0,int(p[0]/t.width*w)))
                    y=min(h-1,max(0,int(p[1]/t.height*w*t.height/t.width)))
                    strength=int(mask[y,x])
                    if strength<=4: continue
                choices.append((-strength,area,zone['id']))
        return min(choices)[2] if choices else None

    def packet_zone(self, packet):
        zone_id=self.names.get(self.normal(packet.get('name','')))
        if zone_id is None or (zone_id in CITIES and self.catalog.require_navigation(zone_id)):
            raise NavigationError('Live zone is unsupported for outdoor continent routing.')
        zone=self.zones[zone_id]
        profile=getattr(self.catalog,'profile',None)
        current=profile and profile['zones'].get(str(zone_id))
        self.catalog.require_navigation(zone_id,packet)
        if packet.get('world') is None or packet.get('continent')!=zone['continent']:
            raise NavigationError('Live continent does not match the zone map. Movement withheld.')
        t=self.transform(zone_id)
        expected=t.world((packet['x']*t.width,packet['y']*t.height),0.)
        error=distance(packet['world'],expected)
        if not math.isfinite(error) or error>1.:
            label=profile['client_version'] if current else '1.12'
            raise NavigationError(f'Live/{label} map alignment differs by {error:.1f} yards. Movement withheld.')
        return zone_id,error

    def require_continent_navigation(self, continent):
        # Geometry selection is per zone, rather than a continent-wide block.
        return

    def roads(self, store, continent):
        """Load each zone's persisted curves/samples in common world XY."""
        from road_curves import flatten
        strokes=[];owners=[]
        for zone in self.zones.values():
            if zone['continent']!=continent or (zone['id'] in CITIES and self.catalog.require_navigation(zone['id'])): continue
            try: roads,curves=store.load_document(zone['id'])
            except (ValueError,OSError) as error:
                raise NavigationError(f'Could not read saved roads for {zone["name"]}: {error}') from error
            if not roads: continue
            t=self.transform(zone['id'])
            for road,curve in zip(roads,curves):
                if curve is not None: road=flatten(curve,t.width,t.height)
                strokes.append([t.world((p[0]*t.width,p[1]*t.height),0.)[:2] for p in road])
                owners.append(zone['id'])
        return strokes,owners
