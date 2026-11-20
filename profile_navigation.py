"""Saved-road navigation for updated zones, legacy collision elsewhere."""
import math

import numpy as np

from core import distance
from navigation import NavigationError, ZoneNavigation, simplify_ordered


class RoadOnlyEngine:
    """Horizontal road following, with no terrain or obstacle claims."""
    @staticmethod
    def point(world):
        if len(world)!=3 or not all(math.isfinite(v) for v in world):
            raise NavigationError('World position must be finite.')
        return tuple(world)

    def nearest(self, world, horizontal=1., vertical=2.):
        return self.point(world)

    def infer_ground(self, world, previous=None):
        if previous is not None and distance(previous,world)>5.:
            raise NavigationError('Unexpected world-position jump. Movement stopped.')
        p=self.point(world)
        return (p[0],p[1],previous[2] if previous is not None else 0.)

    def trace(self, a, b):
        self.point(a)
        return self.point(b)

    def path(self, a, b):
        return [self.point(a),self.point(b)]

    def inset(self, world, margin=.8):
        return self.point(world)

    def clearance(self, world):
        self.point(world)
        return float('inf')

    def transition_clear(self, a, b):
        return distance(a,b)<=5.


class RoadOnlyNavigation(ZoneNavigation):
    def __init__(self, catalog, transform):
        self.catalog=catalog;self.transform=transform;self.engine=RoadOnlyEngine()
        self.tiles=[];self.digest=f'roads-only:{catalog.profile["profile_id"]}:{transform.area_id}'
        self.minimum_radius=0.;self.overlay_scale=1
        self.overlay=np.zeros((transform.height,transform.width,4),np.uint8)
        self.display_bounds=(-4.,5.)

    def collision_checked(self, world=None):
        return False

    def validate_packet(self, packet, previous=None):
        self.catalog.require_navigation(self.transform.area_id,packet)
        return super().validate_packet(packet,previous=previous)

    def jump_clear(self, world, facing, speed, follower):
        return False


def load_zone_navigation(catalog, zone_id, shape, stop=None, atlas=None):
    if catalog.require_navigation(zone_id):
        nav=ZoneNavigation.load(zone_id,shape,stop)
        if catalog.profile: nav.display_bounds=(-4.,5.)
        return nav
    if atlas is None:
        from atlas import Atlas
        atlas=Atlas(catalog)
    return RoadOnlyNavigation(catalog,atlas.transform(zone_id))


class MixedEngine:
    """Split every world-space query at zone boundaries before selecting geometry.

    Legacy loading is lazy so journeys entirely within updated maps need no tiles.
    An error in an unchanged zone propagates; it never enables road-only fallback.
    """
    def __init__(self, atlas, continent, load_legacy):
        self.atlas=atlas;self.continent=continent;self.load_legacy=load_legacy
        self._legacy=None;self.roads=RoadOnlyEngine()
        self.updated={int(z) for z in atlas.catalog.profile['zones'] if atlas.zones[int(z)]['continent']==continent}
        self.updated_bounds=[atlas.zones[z]['bounds'] for z in self.updated]
        self.has_legacy_zones=any(zone['continent']==continent and zone_id not in self.updated
                                  for zone_id,zone in atlas.zones.items())

    @property
    def legacy(self):
        if self._legacy is None: self._legacy=self.load_legacy()
        return self._legacy

    def checked(self, world):
        if not self.has_legacy_zones: return False
        if not any(bottom<=world[0]<=top and right<=world[1]<=left
                   for left,right,top,bottom in self.updated_bounds): return True
        zone=self.atlas.zone_at(self.continent,world)
        # Highlight art can leave holes at a changed map edge. Use the current
        # map bounds there; a positively identified unchanged zone still wins.
        return zone is not None and zone not in self.updated

    def spans(self, a, b):
        RoadOnlyEngine.point(a);RoadOnlyEngine.point(b)
        # Sample each short span, including both endpoints. A changed zone must
        # not disable collision along a segment that crosses an unchanged zone.
        count=max(1,math.ceil(distance(a,b)/.25))
        start=previous=a;mode=self.checked(a)
        for i in range(1,count+1):
            end=tuple(a[k]+(b[k]-a[k])*i/count for k in range(3))
            next_mode=self.checked(end)
            if next_mode!=mode:
                low,high=previous,end
                for _ in range(12):
                    middle=tuple((x+y)/2 for x,y in zip(low,high))
                    if self.checked(middle)==mode: low=middle
                    else: high=middle
                boundary=tuple((x+y)/2 for x,y in zip(low,high))
                yield start,boundary,mode
                start=boundary;mode=next_mode
                if i==count: yield start,end,mode
            elif i==count:
                yield start,end,mode
            previous=end

    def nearest(self, world, horizontal=1., vertical=2.):
        if not self.checked(world): return self.roads.nearest(world,horizontal,vertical)
        try: return self.legacy.nearest(world,horizontal,vertical)
        except NavigationError:
            # A road-only section has no altitude. Resolve a unique legacy
            # surface when re-entering coverage; ambiguity still stops routing.
            return self.legacy.infer_ground(world)

    def infer_ground(self, world, previous=None):
        if previous is not None and distance(previous,world)>5.:
            raise NavigationError('Unexpected world-position jump. Movement stopped.')
        if not self.checked(world): return self.roads.infer_ground(world,previous)
        anchor=previous if previous is not None and self.checked(previous) else None
        return self.legacy.infer_ground(world,previous=anchor)

    def trace(self, a, b):
        current=a
        previous_mode=self.checked(a)
        for start,end,checked in self.spans(a,b):
            if checked:
                if not previous_mode:
                    # Entering legacy coverage has no verified prior altitude.
                    try: current=self.legacy.infer_ground(start)
                    except NavigationError: return None
                projected=self.legacy.trace(current,(*end[:2],current[2]))
                if projected is None: return None
                current=projected
            else:
                current=(*end[:2],current[2])
            previous_mode=checked
        return current

    def path(self, a, b):
        spans=list(self.spans(a,b))
        if len(spans)==1 and spans[0][2]:
            # Both heights were resolved by the caller. Preserve the legacy
            # path query instead of re-inferring an ambiguous bridge endpoint.
            return self.legacy.path(a,b)
        result=[a]
        previous_mode=self.checked(a)
        for start,end,checked in spans:
            if checked:
                begin=self.legacy.infer_ground(start) if not previous_mode else result[-1]
                goal=(b if end==b and self.checked(b) else
                      self.legacy.nearest((*end[:2],begin[2]),horizontal=.35,vertical=24.))
                piece=self.legacy.path(begin,goal)
                # A Detour detour must remain in collision-enabled zones.
                if any(not self.checked(p) for p in piece[1:-1]):
                    raise NavigationError('Legacy detour crosses an updated map; draw a road across the boundary.')
                result.extend(piece[1:])
            else:
                result.append((*end[:2],result[-1][2]))
            previous_mode=checked
        return result

    def inset(self, world, margin=.8):
        return self.legacy.inset(world,margin) if self.checked(world) else self.roads.inset(world,margin)

    def clearance(self, world):
        return self.legacy.clearance(world) if self.checked(world) else self.roads.clearance(world)

    def transition_clear(self, a, b):
        if distance(a,b)>5.: return False
        if self.checked(a) and self.checked(b) and all(checked for _,_,checked in self.spans(a,b)):
            return self.legacy.transition_clear(a,b)
        return self.trace(a,b) is not None


class MixedNavigationMixin:
    def collision_checked(self, world=None):
        return self.engine.checked(world) if world is not None else False

    def validate_polyline(self, points):
        result=[points[0]]
        for a,b in zip(points,points[1:]):
            steps=max(1,math.ceil(distance(a,b)/.5))
            for i in range(1,steps+1):
                end=(a[0]+(b[0]-a[0])*i/steps,a[1]+(b[1]-a[1])*i/steps,result[-1][2])
                projected=self.engine.trace(result[-1],end)
                if projected is None or self.engine.clearance(projected)<.019:
                    raise NavigationError('Path crosses blocked or missing legacy ground.')
                result.append(projected)
            if self.engine.checked(b) and abs(result[-1][2]-b[2])>1.5:
                raise NavigationError('Path ended on a different height layer.')
        return result

    def safe_smooth(self, route):
        # Use the same speed-aware arcs, with every segment checked in its zone.
        return self.round_corners(self.validate_polyline(simplify_ordered(route,min(.5,getattr(self,'road_half_width',.4)))))

    def jump_clear(self, world, facing, speed, follower):
        # Automatic jumping requires a fully checked arc, unavailable when a
        # continent journey contains map sections without collision coverage.
        return False
