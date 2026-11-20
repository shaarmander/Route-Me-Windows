"""Reusable per-zone road drawings and bidirectional network routing.

Saved coordinates are normalized map positions. Only the final terrain planner
can turn a road-network journey into an executable path.
"""
from dataclasses import dataclass
import heapq
import itertools
import json
import math
from pathlib import Path

from core import distance, heading, angle_error
from navigation import NavigationError, PlannedRoute, segment_projection, simplify_ordered
from road_curves import validate_curve


class RoadStore:
    def __init__(self, root=None, transforms=None, legacy_transforms=None, profile_id=None):
        self.root=Path(root or Path(__file__).resolve().parent/'roads.local')
        self.transforms=transforms
        self.legacy_transforms=legacy_transforms
        self.profile_id=profile_id

    def path(self, zone_id):
        if isinstance(zone_id,bool) or not isinstance(zone_id,int) or zone_id<=0:
            raise ValueError('Select a bundled zone map before drawing roads.')
        return self.root/f'{zone_id}.json'

    @staticmethod
    def validate(roads, bounds=(0.,1.)):
        if not isinstance(roads,list) or len(roads)>2000:
            raise ValueError('Invalid road network.')
        result=[];count=0
        for road in roads:
            if not isinstance(road,list) or len(road)<2:
                raise ValueError('Each road needs at least two points.')
            clean=[]
            for p in road:
                if not isinstance(p,(list,tuple)) or len(p)!=2 or any(
                        isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) or not bounds[0]<=v<=bounds[1] for v in p):
                    raise ValueError('Road points must be finite positions inside the map.')
                point=tuple(map(float,p))
                if not clean or distance(clean[-1],point)>1e-10: clean.append(point)
            if len(clean)<2: raise ValueError('Draw a road with some distance.')
            count+=len(clean)
            if count>20000: raise ValueError('Road network has too many points; split it into simpler strokes.')
            result.append(clean)
        return result

    def validate_display_roads(self, roads):
        # Reprojected roads may cross a new map edge. Retain their world geometry;
        # rendering clips at the viewport and new mouse anchors stay on the map.
        return self.validate(roads, (-4.,5.) if self.transforms else (0.,1.))

    @staticmethod
    def map_document(roads, curves, point):
        mapped_roads=[[point(p) for p in road] for road in roads]
        mapped_curves=[[{**node, **{key:point(node[key]) for key in ('point','incoming','outgoing')}}
                        for node in curve] if curve is not None else None for curve in curves]
        return mapped_roads,mapped_curves

    @staticmethod
    def to_world(transform, p):
        return transform.world((p[0]*transform.width,p[1]*transform.height),0.)[:2]

    @staticmethod
    def from_world(transform, p):
        x,y=transform.pixel(p)
        return x/transform.width,y/transform.height

    def import_legacy(self, zone_id, roads, curves):
        if not self.transforms or not self.legacy_transforms:
            raise ValueError('Road migration requires both source and target transforms.')
        old,new=self.legacy_transforms(zone_id),self.transforms(zone_id)
        roads,curves=self.map_document(roads,curves,
            lambda p:self.from_world(new,self.to_world(old,p)))
        return self.save(zone_id,roads,curves)

    def load(self, zone_id):
        return self.load_document(zone_id)[0]

    def load_document(self, zone_id):
        path=self.path(zone_id)
        if not path.exists(): return [],[]
        data=json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(data,dict) or type(data.get('version')) is not int or data.get('version') not in (1,2,3) or data.get('zone_id')!=zone_id:
            raise ValueError('Road file does not match this zone or format.')
        world=data['version']==3
        if world and (data.get('coordinates')!='world' or not self.transforms):
            raise ValueError('World-coordinate roads require a map transform.')
        roads=self.validate(data.get('roads'),(-100000.,100000.) if world else (0.,1.))
        curves=data.get('curves',[None]*len(roads))
        if not isinstance(curves,list) or len(curves)!=len(roads): raise ValueError('Road curves do not match the road network.')
        curves=[validate_curve(c,(-100000.,100000.),(-100000.,100000.)) if world and c is not None
                else validate_curve(c) if c is not None else None for c in curves]
        if world:
            roads,curves=self.map_document(roads,curves,lambda p:self.from_world(self.transforms(zone_id),p))
        elif self.transforms and self.legacy_transforms:
            old,new=self.legacy_transforms(zone_id),self.transforms(zone_id)
            roads,curves=self.map_document(roads,curves,lambda p:self.from_world(new,self.to_world(old,p)))
        if self.transforms:
            roads=self.validate_display_roads(roads)
            curves=[validate_curve(c,(-4.,5.),(-20.,20.)) if c is not None else None for c in curves]
        return roads,curves

    def save(self, zone_id, roads, curves=None):
        path=self.path(zone_id);roads=self.validate_display_roads(roads)
        data=dict(version=1,zone_id=zone_id,roads=roads)
        if curves is not None:
            if len(curves)!=len(roads): raise ValueError('Road curves do not match the road network.')
            bounds=(-4.,5.) if self.transforms else (0.,1.)
            handles=(-20.,20.) if self.transforms else (-4.,5.)
            data.update(version=2,curves=[validate_curve(c,bounds,handles) if c is not None else None for c in curves])
        if self.transforms:
            world_roads,world_curves=self.map_document(roads,data.get('curves',[None]*len(roads)),
                lambda p:self.to_world(self.transforms(zone_id),p))
            self.validate(world_roads,(-100000.,100000.))
            world_curves=[validate_curve(c,(-100000.,100000.),(-100000.,100000.)) if c is not None else None for c in world_curves]
            data.update(version=3,coordinates='world',profile_id=self.profile_id,roads=world_roads,curves=world_curves)
        path.parent.mkdir(parents=True,exist_ok=True)
        temporary=path.with_suffix('.tmp')
        temporary.write_text(json.dumps(data,indent=2)+'\n',encoding='utf-8')
        temporary.replace(path)
        return roads


def project(point,a,b):
    _,t=segment_projection(point,a,b)
    return t,(a[0]+(b[0]-a[0])*t,a[1]+(b[1]-a[1])*t)


class RoadWeldPolicy:
    """Map-resolution-aware junctions and aligned continuations at zone borders."""
    def __init__(self, owners, scales, bounds, border_gap=120.):
        self.owners=owners;self.scales=scales;self.bounds=bounds;self.border_gap=border_gap

    @staticmethod
    def outward(road, side):
        points=road if side==0 else list(reversed(road))
        start=points[0]
        other=next((p for p in points[1:] if distance(p,start)>=8.),points[-1])
        return (start[0]-other[0],start[1]-other[1])

    @staticmethod
    def points_toward(direction, a, b, degrees):
        dx,dy=b[0]-a[0],b[1]-a[1]
        length=math.hypot(*direction)*math.hypot(dx,dy)
        return length>1e-8 and (direction[0]*dx+direction[1]*dy)/length>=math.cos(math.radians(degrees))

    def shared_border(self, source, target, a, b):
        sa,ta=self.bounds[source],self.bounds[target]
        # Bounds may overlap because zone maps include their neighboring roads.
        # The intersection (or the narrow gap between rectangles) defines the
        # border band; distant roads inside separate zones cannot weld.
        lo=(max(sa[3],ta[3]),max(sa[1],ta[1]))
        hi=(min(sa[2],ta[2]),min(sa[0],ta[0]))
        if any(low-high>self.border_gap for low,high in zip(lo,hi)): return False
        for p in (a,b):
            if any(v<min(low,high)-self.border_gap or v>max(low,high)+self.border_gap
                   for v,low,high in zip(p,lo,hi)): return False
        return True

    def allows(self, roads, road_id, side, target_id, a, b, baseline):
        gap=distance(a,b)
        if gap<=baseline: return True
        source,target=self.owners[road_id],self.owners[target_id]
        local=max(baseline,min(24.,4*max(self.scales[source],self.scales[target])))
        target_side=next((s for s,p in enumerate((roads[target_id][0],roads[target_id][-1]))
                          if distance(p,b)<=min(3.,local*.5)),None)
        long=gap>local
        border_limit=min(self.border_gap,32*max(self.scales[source],self.scales[target]))
        if long and (gap>border_limit or source==target or target_side is None or not self.shared_border(source,target,a,b)):
            return False
        degrees=55. if long else 70.
        if not self.points_toward(self.outward(roads[road_id],side),a,b,degrees): return False
        return target_side is None or self.points_toward(self.outward(roads[target_id],target_side),b,a,degrees)


class RoadGraph:
    """Split road segments at crossings and nearby stroke endpoints, in yards."""
    def __init__(self, roads, junction_yards=3., stop=None, weld_policy=None, validate_link=None):
        self.stop=stop
        self.welds=[];self.weld_errors=[]
        segments=[];owners=[];endpoints=[]
        for road_id,road in enumerate(roads):
            self.check_cancelled()
            endpoints.extend(((road_id,0,road[0]),(road_id,1,road[-1])))
            for a,b in zip(road,road[1:]):
                if distance(a,b)>1e-6:
                    segments.append((a,b));owners.append(road_id)
        if not segments: raise NavigationError('No saved roads on this map. Choose Draw roads and trace a road first.')
        cuts=[{0.,1.} for _ in segments]
        links=[]
        for i,(a,b) in enumerate(segments):
            self.check_cancelled()
            # Bounding boxes avoid most comparisons on a large map.
            for j in range(i+1,len(segments)):
                c,d=segments[j]
                if (max(a[0],b[0])+1e-6<min(c[0],d[0]) or max(c[0],d[0])+1e-6<min(a[0],b[0]) or
                    max(a[1],b[1])+1e-6<min(c[1],d[1]) or max(c[1],d[1])+1e-6<min(a[1],b[1])): continue
                ux,uy=b[0]-a[0],b[1]-a[1];vx,vy=d[0]-c[0],d[1]-c[1]
                denominator=ux*vy-uy*vx
                if abs(denominator)>1e-10:
                    wx,wy=c[0]-a[0],c[1]-a[1]
                    t=(wx*vy-wy*vx)/denominator;s=(wx*uy-wy*ux)/denominator
                    if -1e-8<=t<=1+1e-8 and -1e-8<=s<=1+1e-8:
                        cuts[i].add(max(0.,min(1.,t)));cuts[j].add(max(0.,min(1.,s)))
                else:
                    for point in (c,d):
                        t,p=project(point,a,b)
                        if distance(point,p)<1e-6: cuts[i].add(t)
                    for point in (a,b):
                        t,p=project(point,c,d)
                        if distance(point,p)<1e-6: cuts[j].add(t)
        for road_id,side,point in endpoints:
            self.check_cancelled()
            # A stroke may stop just short of another road. Keep a short
            # connector rather than moving its saved shape or joining long gaps.
            candidates={}
            for i,(a,b) in enumerate(segments):
                if owners[i]==road_id: continue
                t,p=project(point,a,b)
                proposals=[(t,p)]
                if weld_policy: proposals.extend(((0.,a),(1.,b)))
                for mark,target in proposals:
                    gap=distance(point,target)
                    if gap<=1e-6: continue
                    allowed=(weld_policy.allows(roads,road_id,side,owners[i],point,target,junction_yards)
                             if weld_policy else gap<=junction_yards)
                    if allowed: candidates.setdefault(owners[i],[]).append((gap,i,mark,target))
            for target_id,options in candidates.items():
                seen=set()
                for gap,i,mark,target in sorted(options):
                    key=tuple(round(v,6) for v in target)
                    if key in seen: continue
                    seen.add(key)
                    self.check_cancelled()
                    try:
                        path=validate_link(point,target) if validate_link else [point,target]
                    except NavigationError as error:
                        self.weld_errors.append(str(error));continue
                    if path is None: continue
                    cuts[i].add(mark);links.append(path)
                    self.welds.append(dict(source=road_id,target=target_id,gap_yards=gap,path=path))
                    # One verified connection per endpoint/target stroke avoids
                    # many redundant joins along a densely sampled curve.
                    break
        self.nodes=[];self.adj={};self.lookup={};self.edge_owners={}
        for (a,b),marks,owner in zip(segments,cuts,owners):
            self.check_cancelled()
            ordered=sorted(marks)
            for t,s in zip(ordered,ordered[1:]):
                p=(a[0]+(b[0]-a[0])*t,a[1]+(b[1]-a[1])*t)
                q=(a[0]+(b[0]-a[0])*s,a[1]+(b[1]-a[1])*s)
                self.edge(self.node(p),self.node(q),owner)
        for path in links:
            for a,b in zip(path,path[1:]): self.edge(self.node(a),self.node(b))
        self.edges=[(a,b) for a,neighbors in self.adj.items() for b in neighbors if a<b]

    def check_cancelled(self):
        if self.stop is not None and self.stop.is_set():
            raise NavigationError('Route planning cancelled.')

    def node(self, point):
        key=tuple(round(v,6) for v in point)
        if key not in self.lookup:
            self.lookup[key]=len(self.nodes);self.nodes.append(point);self.adj[len(self.nodes)-1]={}
        return self.lookup[key]

    def edge(self, a, b, owner=None):
        if a!=b:
            length=distance(self.nodes[a],self.nodes[b])
            self.adj[a][b]=length;self.adj[b][a]=length
            if owner is not None: self.edge_owners.setdefault(tuple(sorted((a,b))),set()).add(owner)

    def entrances(self, point, allowed_edges=None):
        result=[];seen=set()
        for a,b in self.edges:
            if allowed_edges is not None and (a,b) not in allowed_edges: continue
            t,p=project(point,self.nodes[a],self.nodes[b])
            key=tuple(round(v,6) for v in p)
            if key in seen: continue
            seen.add(key);result.append((distance(point,p),a,b,t,p))
        return sorted(result)

    def journey(self, entrance, destination, allowed_edges=None, comfort=False, incoming=None):
        if comfort:
            yield from self.comfortable_journey(entrance,destination,allowed_edges,incoming)
            return
        _,a,b,t,start=entrance
        costs={a:distance(start,self.nodes[a]),b:distance(start,self.nodes[b])};parents={a:None,b:None}
        queue=[(cost,node) for node,cost in costs.items()];heapq.heapify(queue)
        while queue:
            self.check_cancelled()
            cost,node=heapq.heappop(queue)
            if cost!=costs[node]: continue
            for neighbor,length in self.adj[node].items():
                next_cost=cost+length
                if next_cost<costs.get(neighbor,float('inf')):
                    costs[neighbor]=next_cost;parents[neighbor]=node;heapq.heappush(queue,(next_cost,neighbor))
        # Leave at the nearest point on the connected network that was entered.
        # Independent drawings are never bridged by an invented road edge.
        exits=[candidate for candidate in self.entrances(destination,allowed_edges) if candidate[1] in costs]
        if not exits: raise NavigationError('This road has no connected exit.')
        for exit in exits:
            _,c,d,s,end=exit
            choices=[(costs[c]+distance(self.nodes[c],end),c),(costs[d]+distance(self.nodes[d],end),d)]
            length,last=min(choices)
            points=[];node=last
            while node is not None:
                points.append(self.nodes[node]);node=parents[node]
            points=[start]+list(reversed(points))+[end]
            if (a,b)==(c,d) and distance(start,end)<=length:
                points=[start,end];length=distance(start,end)
            clean=[points[0]]
            for p in points[1:]:
                if distance(clean[-1],p)>1e-6: clean.append(p)
            yield clean,length

    def comfortable_journey(self, entrance, destination, allowed_edges, incoming):
        """Directed-edge states retain the approach angle at each junction."""
        _,a,b,_,start=entrance
        costs={};parents={};queue=[];serial=itertools.count()
        for node in (a,b):
            length=distance(start,self.nodes[node])
            turn=turn_cost(incoming,start,self.nodes[node]) if incoming is not None else 0.
            key=(-1,node);costs[key]=length+turn;parents[key]=None
            heapq.heappush(queue,(costs[key],next(serial),key))
        while queue:
            self.check_cancelled()
            cost,_,key=heapq.heappop(queue)
            if cost!=costs[key]: continue
            previous,node=key
            before=start if previous==-1 else self.nodes[previous]
            if previous==-1 and distance(start,self.nodes[node])<1e-5 and incoming is not None:
                before=incoming
            for neighbor,length in self.adj[node].items():
                if neighbor==previous: continue
                next_key=(node,neighbor)
                new_cost=cost+length+turn_cost(before,self.nodes[node],self.nodes[neighbor])
                if new_cost<costs.get(next_key,float('inf')):
                    costs[next_key]=new_cost;parents[next_key]=key
                    heapq.heappush(queue,(new_cost,next(serial),next_key))
        by_node={}
        for key in costs: by_node.setdefault(key[1],[]).append(key)
        for _,c,d,_,end in self.entrances(destination,allowed_edges):
            choices=[]
            for node in (c,d):
                for key in by_node.get(node,[]):
                    before=start if key[0]==-1 else self.nodes[key[0]]
                    cost=costs[key]+distance(self.nodes[node],end)+turn_cost(before,self.nodes[node],end)
                    choices.append((cost,key))
            if not choices: continue
            _,last=min(choices)
            points=[];key=last
            while key is not None:
                points.append(self.nodes[key[1]]);key=parents[key]
            points=[start]+list(reversed(points))+[end]
            if (a,b)==(c,d):
                direct=[start,end]
                if path_comfort(direct)<=path_comfort(points): points=direct
            clean=[points[0]]
            for p in points[1:]:
                if distance(clean[-1],p)>1e-6: clean.append(p)
            yield clean,sum(distance(x,y) for x,y in zip(clean,clean[1:]))


@dataclass
class RoadJourney:
    route: PlannedRoute
    entrance: tuple
    exit: tuple
    road_yards: float


def turn_cost(a,b,c):
    if distance(a,b)<1e-5 or distance(b,c)<1e-5: return 0.
    return 4.*(abs(angle_error(heading(b,c),heading(a,b)))/90.)**2


def path_comfort(points):
    simple=simplify_ordered(points,.1)
    return (sum(distance(a,b) for a,b in zip(points,points[1:]))+
            sum(turn_cost(a,b,c) for a,b,c in zip(simple,simple[1:],simple[2:])))


def choose_road_journey(graph, nav, start, goal, target, packet, entrances=None, exits=None):
    """Compare whole journeys, with bounded terrain queries and strong road preference."""
    from collections import Counter
    t=nav.transform;failures=Counter();candidates=[];access_cache={};leave_cache={}
    report=getattr(nav,'planning_report',{})
    nav.planning_report=report
    initial_approach=None
    if packet.get('facing') is not None:
        yaw=math.radians(packet['facing'])
        initial_approach=(start[0]-math.cos(yaw),start[1]+math.sin(yaw))

    def connector(a,b):
        if distance(a,b)<.01: return nav.validate_polyline([a,b])
        piece=nav.safe_smooth(nav.repair_passage(nav.engine.path(a,b)))
        return piece+nav.validate_polyline([piece[-1],b])[1:]

    for entrance in graph.entrances(start,allowed_edges=entrances)[:16]:
        graph.check_cancelled()
        try:
            entry=nav.engine.infer_ground((*entrance[-1],start[2]))
            access=connector(start,entry)
            access_cache[entrance[-1]]=(entry,access)
        except NavigationError as error:
            failures[str(error)]+=1;continue
        incoming=access[-2][:2] if len(access)>1 and distance(access[-2],entry)>1e-5 else initial_approach
        for guides,_ in itertools.islice(graph.journey(entrance,target,exits,comfort=True,incoming=incoming),16):
            # Lower-cost proposals are verified first, but the first success
            # no longer wins. Off-road travel costs eight times road travel.
            estimate=path_comfort(access+guides[1:]+[goal])+7*(
                sum(distance(a,b) for a,b in zip(access,access[1:]))+distance(guides[-1],goal))
            candidates.append((estimate,entrance[-1],guides))
    candidates.sort(key=lambda c:c[0])
    best=None;valid=attempts=0;seen=set()
    for _,entry_key,guides in candidates:
        graph.check_cancelled()
        key=tuple(guides)
        if key in seen: continue
        seen.add(key);attempts+=1
        entry,access=access_cache[entry_key]
        try:
            pixels=[t.pixel(p) for p in guides];ep=t.pixel(entry)
            anchor=dict(packet,world=entry,world_z_valid=True,x=ep[0]/t.width,y=ep[1]/t.height)
            highway=nav.plan(pixels,anchor).world if len(pixels)>1 else [entry]
            end=tuple(highway[-1])
            if end not in leave_cache:
                try: leave_cache[end]=connector(end,goal)
                except NavigationError as error: leave_cache[end]=error
            leave=leave_cache[end]
            if isinstance(leave,NavigationError): raise leave
            joined=nav.validate_polyline(access+highway[1:]+leave[1:])
            # Round entry/exit seams too, after checking every original leg.
            world=nav.round_corners(joined)
            if distance(world[-1],target)>1.5:
                raise NavigationError('Destination is blocked. Click walkable ground.')
            access_length=sum(distance(a,b) for a,b in zip(access,access[1:]))
            leave_length=sum(distance(a,b) for a,b in zip(leave,leave[1:]))
            score=path_comfort(world)+7*(access_length+leave_length)
            if initial_approach is not None and len(world)>1:
                score+=turn_cost(initial_approach,world[0],world[1])
            # Sample by accumulated distance (the route itself is densely sampled).
            last=world[0]
            for p in world[1:]:
                length=distance(last,p)
                if length<2. and p!=world[-1]: continue
                if nav.collision_checked(p): score+=length*.35/max(.2,nav.engine.clearance(p))
                last=p
            requested=[t.pixel(start)]+pixels+[t.pixel(target)]
            route=PlannedRoute(world,[t.pixel(p) for p in world],requested,nav.digest)
            journey=RoadJourney(route,ep,t.pixel(highway[-1]),sum(distance(a,b) for a,b in zip(highway,highway[1:])))
            valid+=1
            if best is None or score<best[0]: best=(score,journey)
        except NavigationError as error:
            failures[str(error)]+=1
        if attempts>=64 or valid>=8: break
    report.update(route_attempts=attempts,valid_candidates=valid,route_failures=dict(failures))
    if best is not None:
        report['selected_comfort_cost']=round(best[0],3)
        return best[1]
    detail=failures.most_common(1)[0][0] if failures else 'No connected road reaches the destination.'
    raise NavigationError(detail)


def plan_road_route(roads, nav, destination, packet):
    """Join the nearest reachable road, follow it, then take a checked exit."""
    transform=nav.transform
    if (len(destination)!=2 or not all(math.isfinite(v) for v in destination) or
        not 0<=destination[0]<=transform.width or not 0<=destination[1]<=transform.height):
        raise NavigationError('Choose a finite destination inside the current zone map.')
    start,_=nav.validate_packet(packet)
    normalized=RoadStore.validate(roads,bounds=getattr(nav,'display_bounds',(0.,1.)))
    graph=RoadGraph([[transform.world((p[0]*transform.width,p[1]*transform.height),start[2])[:2]
                      for p in road] for road in normalized])
    target=transform.world(destination,start[2])
    # An arbitrary destination needs an unambiguous ground level, just as a
    # fresh connection does. Do not guess an overlapping bridge/cave floor.
    goal=nav.engine.infer_ground(target)
    try:
        return choose_road_journey(graph,nav,start,goal,target,packet)
    except NavigationError as error:
        raise NavigationError(f'Could not verify a journey using these roads. {error}') from error
