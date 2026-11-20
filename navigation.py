"""Local Classic navigation data. All geometry uses world yards, never image pixels.

Reads WorldMapArea.dbc and CMaNGOS v8 / Detour v7 tiles. No game memory access.
Unknown/missing terrain, liquids, steep flags and partial paths fail closed.
"""
from dataclasses import dataclass
from pathlib import Path
import ctypes as ct
import hashlib
import heapq
import itertools
import json
import math
import struct
import threading

import cv2
import numpy as np

from core import distance, resample, travel_corners

ROOT = Path(__file__).resolve().parent
DEFAULT_DATA = Path(r'C:\World of Warcraft Classic')
NAV_DLL = ROOT/'native/build/Release/route_nav_3.dll'
CITIES = {1497, 1537, 1519, 1637, 1638, 1657}
# Tiles already include character-radius clearance. This is only the minimum
# additional numerical/steering buffer; open ground still prefers 0.8 yard.
MIN_STEERING_CLEARANCE = .02


def segment_projection(point, a, b):
    """Horizontal distance and clamped progress along a world-space segment."""
    dx,dy=b[0]-a[0],b[1]-a[1]
    length2=dx*dx+dy*dy
    t=max(0.,min(1.,((point[0]-a[0])*dx+(point[1]-a[1])*dy)/length2)) if length2 else 0.
    return distance(point,(a[0]+t*dx,a[1]+t*dy)),t


def follows_subpath(points, start, end, tolerance=.75):
    """A shortcut may remove small wobbles, never a bend or a reversal."""
    progress=0.
    for point in points[start+1:end+1]:
        offset,t=segment_projection(point,points[start],points[end])
        if offset>tolerance or t<progress-1e-6:
            return False
        progress=t
    return True


def simplify_ordered(points, tolerance):
    result=[points[0]]
    i=0
    while i<len(points)-1:
        j=min(i+32,len(points)-1)
        while j>i+1 and not follows_subpath(points,i,j,tolerance):
            j-=1
        result.append(points[j])
        i=j
    return result


class NavigationError(ValueError):
    pass


@dataclass(frozen=True)
class ZoneTransform:
    area_id: int
    continent: int
    left: float
    right: float
    top: float
    bottom: float
    width: int
    height: int

    def world(self, pixel, z):
        return (self.top+(self.bottom-self.top)*pixel[1]/self.height,
                self.left+(self.right-self.left)*pixel[0]/self.width, z)

    def pixel(self, world):
        return ((world[1]-self.left)/(self.right-self.left)*self.width,
                (world[0]-self.top)/(self.bottom-self.top)*self.height)

    @property
    def yards_per_pixel(self):
        return max(abs(self.right-self.left)/self.width, abs(self.bottom-self.top)/self.height)

    @classmethod
    def read(cls, path, area_id, shape):
        data = Path(path).read_bytes()
        if len(data)<20:
            raise NavigationError('Truncated WorldMapArea.dbc.')
        magic,count,fields,size,strings = struct.unpack_from('<4s4I',data)
        if magic!=b'WDBC' or fields!=8 or size!=32 or len(data)!=20+count*size+strings:
            raise NavigationError('Expected original Classic WorldMapArea.dbc (8 fields).')
        for i in range(count):
            _,continent,area,_,left,right,top,bottom=struct.unpack_from('<4I4f',data,20+i*size)
            if area==area_id:
                if continent not in (0,1) or left<=right or top<=bottom:
                    break
                return cls(area,continent,left,right,top,bottom,shape[1],shape[0])
        raise NavigationError('No outdoor world-coordinate mapping for this zone.')


@dataclass
class Tile:
    path: Path
    payload: bytes
    vertices: np.ndarray
    polygons: list
    radius: float

    @classmethod
    def read(cls, path):
        data=path.read_bytes()
        if len(data)<120:
            raise NavigationError(f'Truncated navigation tile: {path.name}')
        magic,detour,version,size,liquids=struct.unpack_from('<5I',data)
        if (magic,detour,version)!=(0x4d4d4150,7,8) or size!=len(data)-20:
            raise NavigationError(f'Unsupported navigation tile: {path.name}')
        payload=data[20:]
        header=struct.unpack_from('<15i10f',payload)
        if header[:2]!=(0x444e4156,7):
            raise NavigationError(f'Unsupported Detour header: {path.name}')
        polys,verts=header[6:8]
        if not 0<polys<1000000 or not 0<verts<1000000 or 100+12*verts+32*polys>size:
            raise NavigationError(f'Invalid navigation counts: {path.name}')
        vertices=np.frombuffer(payload,dtype='<f4',count=verts*3,offset=100).reshape(-1,3)
        if not np.isfinite(vertices).all() or not math.isfinite(header[16]) or header[16]<.5:
            raise NavigationError(f'Tile lacks required body clearance: {path.name}')
        polygons=[]
        for i in range(polys):
            p=struct.unpack_from('<I6H6HHBB',payload,100+12*verts+32*i)
            indices=p[1:1+p[-2]]
            if p[-2]>6 or any(v>=verts for v in indices):
                raise NavigationError(f'Invalid navigation polygon: {path.name}')
            if p[-1]>>6==0 and p[-3]==1 and len(indices)>=3:
                polygons.append(indices)
        return cls(path,payload,vertices,polygons,header[16])


class Detour:
    def __init__(self, params, dll=None):
        path=Path(dll or NAV_DLL)
        if not path.is_file():
            raise NavigationError('Build navigation first: run Build Navigation.ps1.')
        self.lib=ct.CDLL(str(path))
        fp=ct.POINTER(ct.c_float)
        self.lib.nav_create.argtypes=[ct.c_char_p,ct.c_int]
        self.lib.nav_create.restype=ct.c_void_p
        self.lib.nav_destroy.argtypes=[ct.c_void_p]
        self.lib.nav_add.argtypes=[ct.c_void_p,ct.c_char_p,ct.c_int]
        self.lib.nav_nearest.argtypes=[ct.c_void_p,fp,fp,fp]
        self.lib.nav_surfaces.argtypes=[ct.c_void_p,fp,fp,ct.c_int]
        self.lib.nav_path.argtypes=[ct.c_void_p,fp,fp,fp,ct.c_int]
        self.lib.nav_trace.argtypes=[ct.c_void_p,fp,fp,fp]
        self.lib.nav_inset.argtypes=[ct.c_void_p,fp,ct.c_float,fp]
        self.lib.nav_clearance.argtypes=[ct.c_void_p,fp]
        self.lib.nav_clearance.restype=ct.c_float
        self.handle=self.lib.nav_create(params,len(params))
        if not self.handle:
            raise NavigationError('Cannot initialize this navigation map.')
        self.lock=threading.RLock()

    def __del__(self):
        if getattr(self,'handle',None):
            self.lib.nav_destroy(self.handle)
            self.handle=None

    @staticmethod
    def vector(world):
        return (ct.c_float*3)(world[1],world[2],world[0])

    @staticmethod
    def world(vector):
        return (float(vector[2]),float(vector[0]),float(vector[1]))

    def add(self, tile):
        if not self.lib.nav_add(self.handle,tile.payload,len(tile.payload)):
            raise NavigationError(f'Cannot load tile {tile.path.name}; check matching Detour version.')

    def nearest(self, world, horizontal=1., vertical=2.):
        result=(ct.c_float*3)()
        with self.lock:
            ok=self.lib.nav_nearest(self.handle,self.vector(world),
                (ct.c_float*3)(horizontal,vertical,horizontal),result)
        if not ok:
            raise NavigationError('No walkable ground here at this height; missing tile or blocked ground.')
        found=self.world(result)
        if distance(found,world)>horizontal or abs(found[2]-world[2])>vertical:
            raise NavigationError('Nearest ground exceeds allowed position/height offset.')
        return found

    def path(self, a, b):
        result=(ct.c_float*(4096*3))()
        with self.lock:
            count=self.lib.nav_path(self.handle,self.vector(a),self.vector(b),result,4096)
        if count<2:
            if distance(a,b)<.01 and abs(a[2]-b[2])<.1:
                return [a,b]
            raise NavigationError('No complete walking path; route crosses disconnected or missing ground.')
        return [self.world(result[i*3:i*3+3]) for i in range(count)]

    def infer_ground(self, world, previous=None):
        """Resolve XY on the uniquely connected surface of a recent observation.

        A fresh connection has no altitude evidence and still needs one level.
        Live callers must discard previous across focus/tracking interruptions.
        """
        if previous is not None and distance(previous,world)>5.:
            raise NavigationError('Unexpected world-position jump. Movement stopped.')
        result=(ct.c_float*(512*3))()
        with self.lock:
            count=self.lib.nav_surfaces(self.handle,self.vector(world),result,512)
        if count<0:
            raise NavigationError('Cannot resolve ground layers at this position.')
        if not count:
            raise NavigationError('No walkable ground at your position; blocked ground or missing coverage.')
        candidates=sorted((self.world(result[i*3:i*3+3]) for i in range(count)),key=lambda p:p[2])
        if previous is not None:
            # Observations can straddle an obstacle corner or land on a mesh
            # boundary. Resolve local connectivity, retaining endpoint heights;
            # a blocked straight ray alone does not mean the surface was lost.
            connected=[]
            for candidate in candidates:
                if self.transition_clear(previous,candidate):
                    connected.append(candidate)
            if not connected:
                raise NavigationError('Lost the connected ground surface from the previous position. Movement stopped.')
            candidates=connected
        # Duplicate polygons at shared edges have effectively the same height.
        # Multiple unrelated floors are fine when continuity identifies one;
        # genuinely ambiguous connected surfaces still cannot be distinguished.
        if candidates[-1][2]-candidates[0][2]>.1:
            if previous is not None:
                raise NavigationError('Multiple connected ground levels remain possible; current surface is uncertain. Movement stopped.')
            raise NavigationError('Multiple ground levels here; addon altitude is unavailable. Move to open ground and Connect again.')
        return min(candidates,key=lambda p:distance(p,world))

    def transition_clear(self, previous, current):
        """Verify two nearby observations on connected ground, allowing a corner.

        This is evidence of surface continuity, not permission to steer straight
        through an obstacle. Future movement still requires an unobstructed ray.
        """
        separation=distance(previous,current)
        if separation>5.:
            return False
        projected=self.trace(previous,current)
        if projected is not None and abs(projected[2]-current[2])<=.1:
            return True
        try:
            path=self.path(previous,current)
        except NavigationError:
            return False
        # Detour must reach both observed surfaces, not snap to another floor.
        if any(distance(actual,expected)>.02 or abs(actual[2]-expected[2])>.1
               for actual,expected in ((path[0],previous),(path[-1],current))):
            return False
        # Only a short local bend is plausible between consecutive samples.
        # Never accept a distant ramp/bridge or a route around a large wall as
        # evidence that the player crossed it in this single observation.
        if sum(distance(a,b) for a,b in zip(path,path[1:]))>separation+.7:
            return False
        return all(segment_projection(p,previous,current)[0]<=.35 for p in path)

    def inset(self, world, margin=.8):
        result=(ct.c_float*3)()
        with self.lock:
            ok=self.lib.nav_inset(self.handle,self.vector(world),margin,result)
        if not ok:
            raise NavigationError('Passage has insufficient steering clearance. Draw through wider ground.')
        return self.world(result)

    def clearance(self, world):
        with self.lock:
            return self.lib.nav_clearance(self.handle,self.vector(world))

    def trace(self, a, b):
        result=(ct.c_float*3)()
        with self.lock:
            ok=self.lib.nav_trace(self.handle,self.vector(a),self.vector(b),result)
        return self.world(result) if ok else None


@dataclass
class PlannedRoute:
    world: list
    pixels: list
    requested: list
    source_digest: str


class ZoneNavigation:
    def collision_checked(self, world=None):
        return True

    def __init__(self, transform, engine, tiles, digest):
        self.transform,self.engine,self.tiles,self.digest=transform,engine,tiles,digest
        self.minimum_radius=min(tile.radius for tile in tiles)
        self.overlay_scale=4
        self.overlay=self.make_overlay()

    @classmethod
    def load(cls, area_id, shape, stop=None, data_root=None, transform=None, tile_bounds=None):
        if area_id in CITIES:
            raise NavigationError('Collision routing currently supports outdoor zones; city layers need separate validation.')
        settings_path=ROOT/'navigation.local.json'
        settings=json.loads(settings_path.read_text()) if settings_path.exists() else {}
        root=Path(data_root or settings.get('data_root',str(DEFAULT_DATA)))
        transform=transform or ZoneTransform.read(root/'dbc/WorldMapArea.dbc',area_id,shape)
        bounds=tile_bounds or (transform.left,transform.right,transform.top,transform.bottom)
        params=(root/f'mmaps/{transform.continent:03d}.mmap').read_bytes()
        if len(params)!=28:
            raise NavigationError('Unsupported map parameters.')
        engine=Detour(params)
        tiles=[]; digest=hashlib.sha256(params)
        # Tile filename conventions differ between extractors. Select by bounds
        # in the actual header, retaining a tile of padding for edge detours.
        padding=534.
        for path in sorted((root/'mmaps').glob(f'{transform.continent:03d}*.mmtile')):
            if stop and stop.is_set():
                raise NavigationError('Navigation loading cancelled.')
            with path.open('rb') as stream:
                head=stream.read(120)
            if len(head)<120:
                raise NavigationError(f'Truncated tile {path.name}')
            h=struct.unpack_from('<15i10f',head,20)
            ymin,_,xmin=h[18:21]; ymax,_,xmax=h[21:24]
            left,right,top,bottom=bounds
            if xmax<bottom-padding or xmin>top+padding or ymax<right-padding or ymin>left+padding:
                continue
            tile=Tile.read(path)
            engine.add(tile); tiles.append(tile)
            digest.update(tile.payload)
        if not tiles:
            raise NavigationError('No extracted navigation tiles cover this zone.')
        return cls(transform,engine,tiles,digest.hexdigest())

    def make_overlay(self):
        t=self.transform
        # A visual aid only. Planning/querying always uses the original 3D mesh.
        scale=self.overlay_scale
        mask=np.zeros((t.height*scale,t.width*scale),np.uint8)
        for tile in self.tiles:
            v=tile.vertices
            px=(v[:,0]-t.left)/(t.right-t.left)*t.width
            py=(v[:,2]-t.top)/(t.bottom-t.top)*t.height
            xy=np.rint(np.column_stack((px,py))*16*scale).astype(np.int32)
            for poly in tile.polygons:
                cv2.fillConvexPoly(mask,xy[list(poly)],255,shift=4)
        overlay=np.zeros((*mask.shape,4),np.uint8)
        overlay[mask==0]=(32,24,42,130)
        overlay[mask!=0]=(54,205,149,45)
        return overlay

    def validate_packet(self, packet, previous=None):
        if packet.get('world') is None:
            raise NavigationError('World position unavailable. Install the updated addon and /reload, then Connect.')
        if packet.get('continent')!=self.transform.continent:
            raise NavigationError('Live continent does not match the extracted navigation map.')
        world=packet['world']
        expected=self.transform.world((packet['x']*self.transform.width,packet['y']*self.transform.height),world[2])
        error=distance(world,expected)
        if not math.isfinite(error) or error>1.0:
            raise NavigationError(f'Live position differs from the selected map by {error:.1f} yards. Movement withheld.')
        # Only offline callers can provide a verified height. The live decoder
        # explicitly marks UnitPosition's third return value as unverified.
        ground=(self.engine.nearest(world,horizontal=.35,vertical=2.)
                if packet.get('world_z_valid',False) else self.engine.infer_ground(world,previous=previous))
        return ground,error

    def plan(self, points, packet):
        if len(points)<2 or any(not math.isfinite(v) for p in points for v in p):
            raise NavigationError('Draw a finite route with at least two points.')
        start,_=self.validate_packet(packet)
        if distance(self.transform.world(points[0],start[2]),start)>3:
            raise NavigationError('Draw from your current marker (within 3 yards).')
        # Remove sub-yard hand jitter, independent of map scale or UI zoom.
        guides=simplify_ordered(points,tolerance=.5/self.transform.yards_per_pixel)
        anchors=resample(guides,spacing=24/self.transform.yards_per_pixel)
        route=[start]
        for previous_pixel,pixel in zip(anchors,anchors[1:]):
            requested=self.transform.world(pixel,route[-1][2])
            guide_start=self.transform.world(previous_pixel,route[-1][2])
            goal=self.engine.nearest(requested,horizontal=10,vertical=24)
            piece=self.engine.path(route[-1],goal)
            length=sum(distance(a,b) for a,b in zip(piece,piece[1:]))
            if length>max(80,4*distance(route[-1],goal)):
                raise NavigationError('Required detour is too far from your drawing; redraw around this obstacle.')
            adjusted=self.repair_passage(piece)
            # A short total path can still wander far sideways. Each local
            # detour must remain in the capsule around its own drawn segment.
            if any(segment_projection(p,guide_start,requested)[0]>10 for p in adjusted):
                raise NavigationError('Obstacle requires a detour more than 10 yards from your drawing; draw around it.')
            route.extend(adjusted[1:])
        # The final clicked destination must be reachable, not silently snapped
        # ten yards away like an intermediate guide point.
        if distance(route[-1],self.transform.world(points[-1],route[-1][2]))>1.5:
            raise NavigationError('Destination is blocked. End the line on walkable ground.')
        route=self.safe_smooth(route)
        return PlannedRoute(route,[self.transform.pixel(p) for p in route],list(points),self.digest)

    def repair_passage(self, piece):
        # Retry only this short passage, preserving the drawn route elsewhere.
        # Every adjusted segment must still trace on connected ground.
        for margin in (.8,.4,.2,.08,MIN_STEERING_CLEARANCE):
            try:
                adjusted=[piece[0]]+[self.engine.inset(p,margin) for p in piece[1:]]
                self.validate_polyline(adjusted)
                return adjusted
            except NavigationError:
                continue
        return self.search_passage(piece)

    def search_passage(self, piece):
        # A fixed lattice can put an entire row on a narrow mesh edge. Retry
        # with a different spacing so valid nearby steps are not excluded by
        # that sampling phase. Both searches retain the same ground checks.
        for step in (.5,.375):
            try:
                return self._search_passage(piece,step)
            except NavigationError as error:
                last_error=error
        raise last_error

    def _search_passage(self, piece, step):
        """Find connected intermediate steps when offset corners cut a wall.

        The local lattice stays within three yards of the original Detour
        passage. Every edge is checked in 3D; nearby unrelated floors cannot join.
        This bounded fallback runs only after the inexpensive inset retries.
        """
        goals=[]
        for margin in (.8,.4,.2,.08,.04):
            try:
                goal=self.engine.inset(piece[-1],margin)
                if distance(goal,piece[-1])<=3 and goal not in goals:
                    goals.append(goal)
            except NavigationError:
                continue
        if not goals:
            raise NavigationError('No walkable destination in this passage after local correction.')
        origin=piece[0]
        serial=itertools.count()
        start=(0,0,round(origin[2]*10))
        states={start:origin};costs={start:0.};parents={}
        queue=[(distance(origin,goals[0]),next(serial),0.,start)]
        checked=0
        while queue and checked<6000:
            _,_,cost,key=heapq.heappop(queue)
            if cost>costs[key]+1e-6:
                continue
            checked+=1;world=states[key]
            for goal in goals:
                if distance(world,goal)>.8: continue
                try:
                    tail=self.validate_polyline([world,goal])
                except NavigationError:
                    continue
                route=[world]
                while key in parents:
                    key=parents[key];route.append(states[key])
                route.reverse()
                return route+tail[1:]
            for dx,dy in ((1,0),(0,1),(-1,0),(0,-1),(1,1),(1,-1),(-1,1),(-1,-1)):
                ix,iy=key[0]+dx,key[1]+dy
                desired=(origin[0]+ix*step,origin[1]+iy*step,world[2])
                if min(segment_projection(desired,a,b)[0] for a,b in zip(piece,piece[1:]))>3:
                    continue
                try:
                    candidate=self.engine.nearest(desired,horizontal=.05,vertical=2.)
                    clearance=self.engine.clearance(candidate)
                    if clearance<MIN_STEERING_CLEARANCE-.001: continue
                    self.validate_polyline([world,candidate])
                except NavigationError:
                    continue
                next_key=(ix,iy,round(candidate[2]*10))
                # Prefer comfortable ground without making extra clearance a
                # prerequisite for reaching a narrow but connected passage.
                new_cost=cost+distance(world,candidate)*(1+.12/max(clearance,.05))
                if new_cost>=costs.get(next_key,float('inf')): continue
                states[next_key]=candidate;parents[next_key]=key;costs[next_key]=new_cost
                estimate=min(distance(candidate,goal) for goal in goals)
                heapq.heappush(queue,(new_cost+estimate,next(serial),new_cost,next_key))
        raise NavigationError('Local correction could not verify a connected walking path through this passage.')

    def safe_smooth(self, route):
        # Preserve the ordered route, even across completely open ground.
        # Walkability alone is not permission to straighten a user's bend.
        simple=[route[0]]; i=0
        while i<len(route)-1:
            j=min(i+32,len(route)-1)
            while j>i+1:
                tolerance=.75 if self.collision_checked(route[i]) and self.collision_checked(route[j]) else min(.75,getattr(self,'road_half_width',.4))
                if not follows_subpath(route,i,j,tolerance):
                    j-=1
                    continue
                try:
                    self.validate_polyline([route[i],route[j]])
                    break
                except NavigationError:
                    j-=1
            simple.append(route[j]); i=j
        return self.round_corners(simple)

    def round_corners(self, route):
        # Full journeys include entry/exit seams. Remove redundant samples
        # before sizing turns, while preserving ordered bends and reversals.
        simple=simplify_ordered(route,.10)
        # Sampling boundaries must not masquerade as short approach segments.
        # Coalesce exact straight runs without removing a bend or reversal.
        straight=[]
        for p in simple:
            while len(straight)>1 and follows_subpath([straight[-2],straight[-1],p],0,2,.001):
                straight.pop()
            straight.append(p)
        simple=straight
        result=[simple[0]]
        speed=getattr(self,'travel_speed',7.)
        rate=getattr(self,'travel_turn_rate',180.)
        for curve,fallback in travel_corners([p[:2] for p in simple],speed,rate,deviation=1.25):
            # Road-only maps have no measured clearance: constrain deviations
            # to the user's road width, with a conservative default.
            def project_piece(points, rounded):
                piece=[result[-1]]
                for point in resample(points,spacing=.25)[1:]:
                    end=(*point,piece[-1][2])
                    projected=self.engine.trace(piece[-1],end)
                    if projected is None or self.engine.clearance(projected)<MIN_STEERING_CLEARANCE-.001:
                        raise NavigationError('Corner has insufficient connected ground.')
                    if rounded and not self.collision_checked(projected):
                        gap=min(segment_projection(projected,a,b)[0] for a,b in zip(fallback,fallback[1:]))
                        if gap>getattr(self,'road_half_width',.4):
                            raise NavigationError('Corner exceeds the configured road width.')
                    piece.append(projected)
                return piece
            try:
                piece=project_piece(curve,True)
            except NavigationError:
                try:
                    piece=project_piece(fallback,False)
                except NavigationError:
                    return self.validate_polyline(route)
            result.extend(piece[1:])
        if (self.collision_checked(simple[-1]) and abs(result[-1][2]-simple[-1][2])>1.5) or self.engine.trace(result[-1],simple[-1]) is None:
            raise NavigationError('Smoothed path ended on a different height layer.')
        return result

    def validate_polyline(self, points):
        result=[points[0]]
        for a,b in zip(points,points[1:]):
            steps=max(1,math.ceil(distance(a,b)/.5))
            for i in range(1,steps+1):
                end=(a[0]+(b[0]-a[0])*i/steps,a[1]+(b[1]-a[1])*i/steps,result[-1][2])
                projected=self.engine.trace(result[-1],end)
                if projected is None or self.engine.clearance(projected)<MIN_STEERING_CLEARANCE-.001:
                    raise NavigationError('Path touches an unverified edge; draw with more clearance.')
                result.append(projected)
            if abs(result[-1][2]-b[2])>1.5 or self.engine.trace(result[-1],b) is None:
                raise NavigationError('Path ended on a different height layer.')
        return result

    def target_clear(self, world, pixel):
        # Use nearby ground at the current elevation, never flatten a bridge.
        try:
            end=self.engine.nearest(self.transform.world(pixel,world[2]),horizontal=.12,vertical=4.)
            self.validate_polyline([world,end])
        except NavigationError:
            return False
        return True

    def rejoin_route(self, world, route, segment):
        """Build a short connected return to the upcoming route after drift."""
        first=min(segment+1,len(route)-1)
        candidates=[first];along=0.;mark=2.
        for i in range(first+1,len(route)):
            along+=distance(route[i-1],route[i])
            if along>8: break
            if along>=mark:
                candidates.append(i);mark+=2.
        for index in candidates:
            goal=route[index]
            if distance(world,goal)>10: continue
            try:
                piece=self.engine.path(world,goal)
                repaired=self.safe_smooth(self.repair_passage(piece))
                # Keep recovery local; do not skip a long road bend or take a
                # distant detour just because it eventually reaches the route.
                if sum(distance(a,b) for a,b in zip(repaired,repaired[1:]))>max(12,3*distance(world,goal)):
                    continue
                if any(distance(p,world)>12 for p in repaired): continue
                # Insetting may move the rejoin point. Check the connection to
                # the original route before appending its untouched remainder.
                join=self.validate_polyline([repaired[-1],goal])
                return repaired+join[1:]+list(route[index+1:])
            except NavigationError:
                continue
        raise NavigationError('Could not find a connected local return to the route from the current position.')

    @staticmethod
    def movement_window(remaining_yards, speed):
        # Brake near the destination: checking a full run beyond the endpoint
        # can reject a valid approach to a wall. Shorten the key lease with it.
        horizon=max(.16,min(.55,4./speed,remaining_yards/speed))
        return horizon,min(.45,horizon-.05)

    def checked_steering(self, world, facing, action, error, rate, duration, speed, horizon, timing_gain=1., forward_seconds=None, route_guard=None):
        candidates=[(action,duration)]
        if action in ('left','right') and abs(error)<=65:
            candidates.insert(0,('forward_'+action,duration))
        if '+' in action:
            candidates.append((action.split('+')[0],duration))
        if abs(error)>.75 and abs(error)<=55:
            turn='right' if error>0 else 'left'
            candidates.append(('forward_'+turn,max(.012,(abs(error)-1.)/rate)*timing_gain))
        if action.startswith('strafe_') and abs(error)<=8:
            candidates.append(('forward',duration))
        for candidate,requested_seconds in candidates:
            for window in dict.fromkeys((horizon,min(horizon,.35),min(horizon,.22),min(horizon,.16))):
                seconds=min(requested_seconds,window-.05)
                if self.motion_clear(world,facing,candidate,rate,input_seconds=seconds,speed=speed,seconds=window,
                                     forward_seconds=min(forward_seconds,window-.05) if forward_seconds is not None else None,
                                     route_guard=route_guard):
                    return candidate,seconds,window
        if abs(error)>3.:
            return ('right' if error>0 else 'left'),max(.012,(abs(error)-2.5)/rate)*timing_gain,horizon
        # Already aligned but no clear forward arc: alternating tiny stationary
        # turns cannot make this ground traversable. Withhold input instead.
        return 'wait',.1,horizon

    def jump_clear(self, world, facing, speed, follower):
        span=speed*1.5
        remaining=(follower.lengths[-1]-follower.progress)*self.transform.yards_per_pixel
        if remaining<span+3: return False
        angle=math.radians(facing);previous=world;heights=[world[2]]
        for i in range(1,math.ceil(span/.5)+1):
            ahead=min(span,i*.5)
            route_pixel=follower.point_at(follower.progress+ahead/self.transform.yards_per_pixel)
            route_world=self.transform.world(route_pixel,world[2])
            bearing=math.degrees(math.atan2(-(route_world[1]-world[1]),route_world[0]-world[0]))%360
            if abs((bearing-facing+180)%360-180)>7: return False
            target=(world[0]+math.cos(angle)*ahead,world[1]-math.sin(angle)*ahead,previous[2])
            ground=self.engine.trace(previous,target)
            if ground is None or self.engine.clearance(ground)<1.25: return False
            heights.append(ground[2])
            if max(heights)-min(heights)>.6: return False
            previous=ground
        return True

    def motion_clear(self, world, facing, action, turn_rate, seconds=.55, input_seconds=.18, speed=7., forward_seconds=None, route_guard=None):
        # Sweep a conservative short horizon for the actual combined inputs.
        # Both sides of the centreline add margin beyond the baked body radius.
        if action in ('arrived','observe','left','right'):
            return True
        p=world
        yaw=facing
        turning=1 if action.split('+')[0]=='forward_right' else -1
        has_turn=action.startswith('forward_')
        lateral=(-1 if 'strafe_left' in action else 1 if 'strafe_right' in action else 0)
        # Split exactly at key-up, with small midpoint arc segments. The old
        # 12-step endpoint integration could approve an arc outside the actual
        # turn, and applied a whole strafe step past its release deadline.
        cuts=sorted(set([0.,seconds,min(seconds,max(0.,input_seconds)),
                         min(seconds,forward_seconds) if forward_seconds is not None else seconds]+
                        [seconds*i/math.ceil(seconds/.01) for i in range(1,math.ceil(seconds/.01))]))
        for begin,end_time in zip(cuts,cuts[1:]):
            dt=end_time-begin
            active=begin<input_seconds
            rotation=turning*turn_rate*dt if has_turn and active else 0.
            side=lateral if active else 0
            angle=math.radians(yaw+rotation/2+math.degrees(math.atan2(side,1)))
            yaw+=rotation
            if forward_seconds is not None and begin>=forward_seconds: continue
            step=speed*dt
            end=(p[0]+math.cos(angle)*step,p[1]-math.sin(angle)*step,p[2])
            if route_guard and not route_guard(end): return False
            projected=self.engine.trace(p,end)
            if projected is None: return False
            clearance=self.engine.clearance(projected)
            if clearance<MIN_STEERING_CLEARANCE-.001: return False
            # Use the space available in this passage instead of reinstating a
            # fixed extra radius that would reject the repaired narrow route.
            probe=min(.2,clearance*.5)
            for side in (-probe,probe):
                edge=(projected[0]+math.sin(angle)*side,projected[1]+math.cos(angle)*side,projected[2])
                if self.engine.trace(projected,edge) is None: return False
            # Detour uses float32. Retain the integrated XY so repeated short
            # sweeps do not accumulate coordinate rounding as artificial drift.
            p=(end[0],end[1],projected[2])
        return True
