"""Same-continent road journeys, preserving world coordinates across zone changes."""
import time
from collections import Counter

import numpy as np

from core import distance
from navigation import NavigationError, ZoneNavigation
from road_network import RoadGraph, RoadWeldPolicy, choose_road_journey
from telemetry import AddonTracker, decode
from profile_navigation import MixedEngine, MixedNavigationMixin


class ContinentTracker(AddonTracker):
    def __init__(self, atlas, continent, center):
        t=atlas.frame(continent)
        super().__init__(None,(t.height,t.width,3),center)
        self.atlas=atlas;self.continent=continent;self.zone_id=None
        self.last_decoded_packet=None

    def locate(self, rgb):
        packet=decode(rgb)
        self.last_decoded_packet=packet  # Diagnostics only; accepted state stays below validation.
        zone_id,_=self.atlas.packet_zone(packet)
        if packet['continent']!=self.continent:
            raise NavigationError('Continent changed. This walking route cannot use boats, portals or transports.')
        now=time.monotonic()
        if packet['sequence']!=self.sequence:
            self.sequence,self.changed=packet['sequence'],now
        elif now-self.changed>.75:
            raise ValueError('Addon data stopped updating.')
        self.packet=packet;self.zone_id=zone_id;self.map_id=packet['map_id']
        self.last=self.atlas.frame(self.continent).pixel(packet['world'])
        return self.last,packet['facing'],1.


class ContinentNavigation(ZoneNavigation):
    def make_overlay(self):
        # Keep continent artwork legible; ground queries still use the 3D mesh.
        self.overlay_scale=1
        return np.zeros((self.transform.height,self.transform.width,4),np.uint8)

    def validate_packet(self, packet, previous=None):
        if packet.get('world_z_valid',False):
            return super().validate_packet(packet,previous=previous)  # Internal checked road anchor.
        _,alignment=self.atlas.packet_zone(packet)
        if packet['continent']!=self.transform.continent:
            raise NavigationError('Live continent changed. Movement withheld.')
        return self.engine.infer_ground(packet['world'],previous=previous),alignment

    @classmethod
    def load_roads(cls, atlas, continent, roads, start, goal, stop=None, data_root=None):
        points=[start,goal]+[p for road in roads for p in road]
        bounds=(max(p[1] for p in points),min(p[1] for p in points),
                max(p[0] for p in points),min(p[0] for p in points))
        t=atlas.frame(continent)
        profile=getattr(getattr(atlas,'catalog',None),'profile',None)
        if profile and any(atlas.zones[int(z)]['continent']==continent for z in profile['zones']):
            nav=MixedContinentNavigation.__new__(MixedContinentNavigation)
            nav.transform=t;nav.atlas=atlas;nav.tiles=[];nav.minimum_radius=0.
            nav.digest=f'mixed-roads:{profile["profile_id"]}';nav.overlay_scale=1
            nav.overlay=nav.make_overlay()
            def load_legacy():
                legacy=cls.load(0,(t.height,t.width,3),stop,data_root,transform=t,tile_bounds=bounds)
                nav.tiles.extend(legacy.tiles)
                nav.digest=f'mixed-roads:{profile["profile_id"]}:{legacy.digest}'
                return legacy.engine
            nav.engine=MixedEngine(atlas,continent,load_legacy)
            nav.display_bounds=(-4.,5.)
            return nav
        nav=cls.load(0,(t.height,t.width,3),stop,data_root,transform=t,tile_bounds=bounds)
        nav.atlas=atlas
        return nav


class MixedContinentNavigation(MixedNavigationMixin, ContinentNavigation):
    pass


def plan_continent_route(roads, owners, nav, destination, destination_zone, packet, stop=None):
    """Enter a source-zone road component that reaches a destination-zone road."""
    source_zone,_=nav.atlas.packet_zone(packet)
    if source_zone not in owners or destination_zone not in owners:
        raise NavigationError('Save roads in both the starting and destination zones before routing between them.')
    start,_=nav.validate_packet(packet)
    t=nav.transform
    policy=RoadWeldPolicy(owners,{z:nav.atlas.transform(z).yards_per_pixel for z in set(owners)},
                          {z:nav.atlas.zones[z]['bounds'] for z in set(owners)})
    cached_links={}
    def weld(a,b):
        key=tuple(sorted((tuple(a),tuple(b))))
        if key in cached_links:
            cached=cached_links[key]
            if isinstance(cached,NavigationError): raise cached
            return cached if distance(cached[0],a)<1e-6 else list(reversed(cached))
        try:
            origin=nav.engine.infer_ground((*a,0.))
            goal=nav.engine.nearest((*b,origin[2]),horizontal=.35,vertical=24.)
            piece=nav.safe_smooth(nav.repair_passage(nav.engine.path(origin,goal)))
            piece=piece+nav.validate_polyline([piece[-1],goal])[1:]
            if sum(distance(x,y) for x,y in zip(piece,piece[1:]))>max(15.,2.5*distance(a,b)):
                raise NavigationError('Road join requires a distant detour; extend the drawing along the passage.')
            result=[tuple(p[:2]) for p in piece]
            # Mesh insetting must not move the shared graph anchor.
            if distance(result[0],a)>.05 or distance(result[-1],b)>.05:
                raise NavigationError('Road join does not reach both drawn endpoints.')
            result[0]=tuple(a);result[-1]=tuple(b)
            cached_links[key]=result
            return result
        except NavigationError as error:
            cached_links[key]=error;raise
    graph=RoadGraph(roads,stop=stop,weld_policy=policy,validate_link=weld)
    nav.planning_report=dict(source_zone=source_zone,destination_zone=destination_zone,
        welds=[dict(source_zone=owners[w['source']],destination_zone=owners[w['target']],
                    gap_yards=round(w['gap_yards'],3)) for w in graph.welds],
        weld_failures=dict(Counter(graph.weld_errors)))
    exits={edge for edge,tags in graph.edge_owners.items() if any(owners[i]==destination_zone for i in tags)}
    entrances={edge for edge,tags in graph.edge_owners.items() if any(owners[i]==source_zone for i in tags)}
    goal=nav.engine.infer_ground(destination)

    reachable={node for edge in entrances for node in edge};pending=list(reachable)
    while pending:
        graph.check_cancelled()
        node=pending.pop()
        for other in graph.adj[node]:
            if other not in reachable: reachable.add(other);pending.append(other)
    connected=any(edge[0] in reachable for edge in exits)
    nav.planning_report['drawings_connected']=connected
    if connected:
        try:
            return choose_road_journey(graph,nav,start,goal,destination,packet,entrances,exits)
        except NavigationError as error:
            graph.check_cancelled()
            raise NavigationError(f'Road drawings are connected, but the route could not be verified: {error}') from error
    if graph.weld_errors:
        detail=Counter(graph.weld_errors).most_common(1)[0][0]
        raise NavigationError(f'No connected road network reaches that zone. A nearby road join failed: {detail}')
    raise NavigationError('No connected road network reaches that zone. Extend the missing road connection; nearby aligned ends are welded automatically.')
