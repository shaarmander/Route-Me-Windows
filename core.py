"""Screen-space route geometry. No game access or keyboard side effects."""
import math
import random
import bisect
from functools import lru_cache


def distance(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def heading(a, b):
    """Degrees clockwise from screen north."""
    return math.degrees(math.atan2(b[0] - a[0], a[1] - b[1])) % 360


def angle_error(target, actual):
    return (target - actual + 180) % 360 - 180


def simplify(points, epsilon=2.0):
    """Ramer–Douglas–Peucker, preserving turns in a freehand path."""
    if len(points) <= 2:
        return list(points)
    a, b = points[0], points[-1]
    dx, dy = b[0] - a[0], b[1] - a[1]
    length2 = dx * dx + dy * dy
    def segment_distance(p):
        t = max(0, min(1, ((p[0]-a[0])*dx + (p[1]-a[1])*dy) / length2)) if length2 else 0
        return distance(p, (a[0] + t*dx, a[1] + t*dy))
    errors = [segment_distance(p) for p in points[1:-1]]
    maximum = max(errors)
    if maximum <= epsilon:
        return [a, b]
    index = errors.index(maximum) + 1
    return simplify(points[:index+1], epsilon)[:-1] + simplify(points[index:], epsilon)


def resample(points, spacing=12):
    result = [points[0]] if points else []
    for a, b in zip(points, points[1:]):
        steps = max(1, math.ceil(distance(a, b) / spacing))
        result.extend((a[0]+(b[0]-a[0])*i/steps, a[1]+(b[1]-a[1])*i/steps)
                      for i in range(1, steps+1))
    return result


def timed_route(points, position, facing, speed, turn_rate):
    """Compile live-map pixels into (key, seconds); no runtime observations."""
    values = [speed, turn_rate, facing, *position, *(v for p in points for v in p)]
    if not all(math.isfinite(v) for v in values) or speed <= 0 or turn_rate <= 0:
        raise ValueError('Speed and turn rate must be positive, finite numbers.')
    if len(points) < 2:
        raise ValueError('Draw at least two route points.')
    if distance(position, points[0]) > 5:
        raise ValueError('Start at the beginning of the drawn route (within 5 map pixels).')
    commands = []
    for target in simplify(points, epsilon=.1)[1:]:
        length = distance(position, target)
        if length < .01:
            continue
        target_heading = heading(position, target)
        error = angle_error(target_heading, facing)
        if abs(error) > .01:
            commands.append(('right' if error > 0 else 'left', abs(error)/turn_rate))
        commands.append(('forward', length/speed))
        position, facing = target, target_heading
    if not commands:
        raise ValueError('Draw a route with some distance.')
    return commands


class Follower:
    def __init__(self, points, tolerance=5, turn_threshold=12, align_threshold=12):
        if len(points) < 2:
            raise ValueError('Draw at least two route points.')
        self.points = resample(simplify(points, epsilon=.1))
        self.index = 0
        self.tolerance = tolerance
        self.turn_threshold = turn_threshold
        self.align_threshold = align_threshold
        self.aligning = True

    def step(self, position, facing):
        while self.index < len(self.points) and distance(position, self.points[self.index]) <= self.tolerance:
            self.index += 1
            self.aligning = True
        if self.index == len(self.points):
            return 'arrived', 0
        error = angle_error(heading(position, self.points[self.index]), facing)
        threshold = self.align_threshold if self.aligning else self.turn_threshold
        if abs(error) > threshold:
            self.aligning = True
            return ('right' if error > 0 else 'left'), error
        self.aligning = False
        return 'forward', error

    def steering(self, position, facing, action, error, walking):
        """Blend forward, turn, and strafe when recovering gently toward a segment."""
        if not walking or self.index<=0 or self.index>=len(self.points):
            return action
        a,b = self.points[self.index-1],self.points[self.index]
        remaining = distance(position,b)
        if remaining<1.5:
            return action
        length = distance(a,b)
        if length<=.01:
            return action
        dx,dy = (b[0]-a[0])/length,(b[1]-a[1])/length
        along = (position[0]-a[0])*dx+(position[1]-a[1])*dy
        sideways = (position[0]-a[0])*(-dy)+(position[1]-a[1])*dx
        tangent_error = angle_error(heading(a,b),facing)
        lateral = 0<=along<=length and .2<abs(sideways)<=1.25
        strafe = 'strafe_left' if sideways>0 else 'strafe_right'
        if lateral and 8<abs(tangent_error)<=30 and action in ('left','right') and abs(error)<=30:
            return 'forward_'+action+'+'+strafe
        if lateral and abs(tangent_error)<=8:
            return strafe
        if action in ('left','right') and abs(error)<=30:
            return 'forward_'+action
        return action


def route_curve(points):
    return list(_route_curve(tuple(tuple(p) for p in points)))


def travel_corners(points, speed=7., turn_rate=180., deviation=.75):
    """Circular-arc Beziers in world yards, split into independently checked pieces.

    Leave an infeasible corner sharp so the motion checker can align stationary.
    Never erase a reversal or borrow more than 40% of an adjacent segment.
    """
    clean=[points[0]]
    for p in points[1:]:
        if distance(clean[-1],p)>1e-6: clean.append(p)
    current=clean[0]
    radius=max(.1,speed/math.radians(turn_rate))
    for a,b,c in zip(clean,clean[1:],clean[2:]):
        before,after=distance(a,b),distance(b,c)
        u=tuple((b[i]-a[i])/before for i in (0,1))
        v=tuple((c[i]-b[i])/after for i in (0,1))
        angle=math.acos(max(-1.,min(1.,sum(x*y for x,y in zip(u,v)))))
        cut=radius*math.tan(angle/2) if angle<math.pi else float('inf')
        offset=radius*(1-math.cos(angle/2))
        if not math.radians(5)<angle<math.radians(150) or cut>min(before,after)*.4 or offset>deviation:
            yield [current,b],[current,b]
            current=b
            continue
        start=tuple(b[i]-u[i]*cut for i in (0,1))
        end=tuple(b[i]+v[i]*cut for i in (0,1))
        handle=4/3*radius*math.tan(angle/4)
        p1=tuple(start[i]+u[i]*handle for i in (0,1))
        p2=tuple(end[i]-v[i]*handle for i in (0,1))
        curve=[current,start]
        for step in range(1,max(4,math.ceil(radius*angle/.2))+1):
            t=step/max(4,math.ceil(radius*angle/.2))
            curve.append(tuple((1-t)**3*start[i]+3*(1-t)**2*t*p1[i]+3*(1-t)*t*t*p2[i]+t**3*end[i] for i in (0,1)))
        yield curve,[current,b,end]
        current=end
    yield [current,clean[-1]],[current,clean[-1]]


@lru_cache(maxsize=16)
def _route_curve(points):
    """Round corners with tangent cubic Beziers, preserving endpoints and hairpins."""
    clean = []
    for point in points:
        if not clean or distance(clean[-1],point)>.001:
            clean.append(point)
    if len(clean)<3:
        return tuple(resample(clean,spacing=.25))
    result=[clean[0]]
    for a,b,c in zip(clean,clean[1:],clean[2:]):
        before,after=distance(a,b),distance(b,c)
        bend=abs(angle_error(heading(b,c),heading(a,b)))
        if bend<2 or bend>150:
            result.extend(resample([result[-1],b],spacing=.25)[1:])
            continue
        cut=min(1.5,before*.3,after*.3)
        p0=tuple(b[j]+(a[j]-b[j])*cut/before for j in (0,1))
        p3=tuple(b[j]+(c[j]-b[j])*cut/after for j in (0,1))
        p1=tuple(p0[j]+(b[j]-p0[j])*2/3 for j in (0,1))
        p2=tuple(p3[j]+(b[j]-p3[j])*2/3 for j in (0,1))
        result.extend(resample([result[-1],p0],spacing=.25)[1:])
        steps=max(4,math.ceil(2*cut/.15))
        for step in range(1,steps+1):
            t=step/steps
            result.append(tuple((1-t)**3*p0[j]+3*(1-t)**2*t*p1[j]+3*(1-t)*t*t*p2[j]+t**3*p3[j] for j in (0,1)))
    result.extend(resample([result[-1],clean[-1]],spacing=.25)[1:])
    return tuple(result)


class SteeringTargetError(ValueError):
    """The route is valid, but no direct approach to its look-ahead is clear."""


class CurveFollower:
    """Follow arc length with local projection and an interpolated look-ahead target."""
    def __init__(self, points, prepared=False, target_guard=None, yards_per_pixel=1.):
        if len(points)<2:
            raise ValueError('Draw at least two route points.')
        self.points=list(points) if prepared else route_curve(points)
        self.prepared=prepared
        self.target_guard=target_guard
        if len(self.points)<2:
            raise ValueError('Draw a route with some distance.')
        self.lengths=[0.]
        for a,b in zip(self.points,self.points[1:]):
            self.lengths.append(self.lengths[-1]+distance(a,b))
        self.progress=0.
        self.segment=0
        self.index=1
        self.tolerance=.35
        self.aligning=True
        self.target=self.points[0]
        self.cross_track=0.
        self.route_distance=0.
        self.tangent=0.
        self.yards_per_pixel=yards_per_pixel
        self.strafing=False
        self.turn_direction=None
        self.initial_alignment=True
        self.movement_speed=7.
        self.turn_rate=180.
        self.previous_position=None

    def point_at(self, progress):
        i=min(len(self.points)-2,max(0,bisect.bisect_right(self.lengths,progress)-1))
        length=self.lengths[i+1]-self.lengths[i]
        t=max(0,min(1,(progress-self.lengths[i])/length)) if length else 0
        return tuple(self.points[i][j]+t*(self.points[i+1][j]-self.points[i][j]) for j in (0,1))

    def locate_progress(self, position):
        best=None
        for i in range(max(0,self.segment-1),len(self.points)-1):
            window=6./self.yards_per_pixel if self.prepared else 3.
            if self.lengths[i]>self.progress+window:
                break
            a,b=self.points[i:i+2]
            dx,dy=b[0]-a[0],b[1]-a[1]
            length2=dx*dx+dy*dy
            if not length2:
                continue
            t=max(0,min(1,((position[0]-a[0])*dx+(position[1]-a[1])*dy)/length2))
            progress=self.lengths[i]+t*math.sqrt(length2)
            if progress<self.progress-max(.01,1./self.yards_per_pixel):
                continue
            projection=(a[0]+t*dx,a[1]+t*dy)
            error=distance(position,projection)
            if best is None or error<best[0]:
                best=(error,progress,i,projection)
        if best:
            self.route_distance,progress,self.segment,projection=best
            self.progress=max(self.progress,progress)
            a,b=self.points[self.segment:self.segment+2]
            length=distance(a,b)
            self.cross_track=((position[0]-projection[0])*(-(b[1]-a[1]))+(position[1]-projection[1])*(b[0]-a[0]))/length
            self.tangent=heading(a,b)
        else:
            self.route_distance=distance(position,self.point_at(self.progress))
        self.index=min(self.segment+1,len(self.points)-1)

    def within_corridor(self, position, yards=1.8):
        """Check a predicted maneuver against the nearby route, without advancing it."""
        end=min(len(self.points)-1,bisect.bisect_right(self.lengths,self.progress+8./self.yards_per_pixel)+1)
        for i in range(max(0,self.segment-1),end):
            a,b=self.points[i:i+2]
            dx,dy=b[0]-a[0],b[1]-a[1]
            length2=dx*dx+dy*dy
            t=max(0.,min(1.,((position[0]-a[0])*dx+(position[1]-a[1])*dy)/length2)) if length2 else 0.
            if distance(position,(a[0]+t*dx,a[1]+t*dy))*self.yards_per_pixel<=yards: return True
        return False

    def step(self, position, facing):
        previous=self.previous_position
        self.previous_position=tuple(position)
        self.locate_progress(position)
        remaining=self.lengths[-1]-self.progress
        passed=False
        if remaining<=self.tolerance and previous is not None:
            # Fast travel can cross the endpoint between two fresh packets.
            # Stop when that verified segment passed through the arrival circle
            # instead of steering back to a target now behind the character.
            dx,dy=position[0]-previous[0],position[1]-previous[1]
            length2=dx*dx+dy*dy
            if length2:
                goal=self.points[-1]
                t=((goal[0]-previous[0])*dx+(goal[1]-previous[1])*dy)/length2
                passed=(0<=t<=1 and distance(goal,(previous[0]+t*dx,previous[1]+t*dy))<=self.tolerance)
        if remaining<=self.tolerance and (distance(position,self.points[-1])<=self.tolerance or passed):
            self.index=len(self.points)
            return 'arrived',0
        # Keep the aim closer on tight bends, farther away on straight sections.
        scale=self.yards_per_pixel
        speed_scale=max(.5,self.movement_speed/7.) if self.prepared else 1.
        near_distance=2.*speed_scale/scale if self.prepared else .5
        far_distance=6.*speed_scale/scale if self.prepared else 1.5
        near=self.point_at(min(self.progress+near_distance,self.lengths[-1]))
        far=self.point_at(min(self.progress+far_distance,self.lengths[-1]))
        bend=abs(angle_error(heading(near,far),self.tangent)) if distance(near,far)>.01 else 0
        # Avoid doubling/halving the aim distance at one bend threshold; that
        # made the requested heading jump while traversing a smooth curve.
        lookahead=((5.-2.5*min(1.,bend/60.))*speed_scale/scale if self.prepared else 1.2-.6*min(1.,bend/60.))
        if self.prepared:
            # Slower angular response needs earlier turn initiation. The
            # visibility and swept-motion checks still decide whether it fits.
            radius=self.movement_speed/math.radians(self.turn_rate)
            lookahead=max(lookahead,min(8.*speed_scale,radius*min(1.5,bend/60.))/scale)
        self.target=self.point_at(min(self.progress+lookahead,self.lengths[-1]))
        if self.target_guard:
            while not self.target_guard(self.target):
                lookahead*=.5
                if lookahead<(.15/scale if self.prepared else .04):
                    raise SteeringTargetError('No clear steering target on the planned surface.')
                self.target=self.point_at(min(self.progress+lookahead,self.lengths[-1]))
        error=angle_error(heading(position,self.target),facing)
        direction='right' if error>0 else 'left'
        if self.turn_direction and (abs(error)<=1.25 or direction!=self.turn_direction):
            self.turn_direction=None
        threshold=3 if self.initial_alignment else 6
        if abs(error)<=3: self.initial_alignment=False
        if abs(error)>threshold:
            self.turn_direction=direction
        if self.turn_direction:
            self.aligning=True
            return self.turn_direction,error
        self.aligning=False
        return 'forward',error

    def steering(self,position,facing,action,error,walking):
        if action=='arrived' or not walking or (self.lengths[-1]-self.progress)*self.yards_per_pixel<.75:
            self.strafing=False
            return action
        tangent_error=abs(angle_error(self.tangent,facing))
        offset=abs(self.cross_track)*self.yards_per_pixel
        lateral=(.1 if self.strafing else .25)<offset<=1.5 and tangent_error<=30
        self.strafing=lateral
        strafe='strafe_left' if self.cross_track>0 else 'strafe_right'
        ahead=heading(self.point_at(self.progress),self.target)
        straight_ahead=abs(angle_error(ahead,self.tangent))<=12
        # If facing along the route, fix lateral drift without rotating toward
        # the offset look-ahead target. That rotation used to suppress strafing.
        if lateral and tangent_error<=8 and abs(error)<=35 and straight_ahead:
            return strafe
        if action in ('left','right') and abs(error)<=55:
            return 'forward_'+action+('+'+strafe if lateral else '')
        return action


class ProgressMonitor:
    """Allow slow screen-space motion; count forward-held time, not turn/wait time."""
    def __init__(self, timeout=20, minimum_distance=1.5):
        self.timeout = timeout
        self.minimum_distance = minimum_distance
        self.anchor = None
        self.previous_time = None
        self.previous_moving = False
        self.stalled_seconds = 0.

    def update(self, position, now, moving):
        if self.anchor is None:
            self.anchor = position
        if self.previous_time is not None and self.previous_moving:
            self.stalled_seconds += max(0., now-self.previous_time)
            if distance(position, self.anchor) >= self.minimum_distance:
                self.anchor = position
                self.stalled_seconds = 0.
        if not moving:
            # Rotation can shift a sprite by a pixel. Do not count that as walking.
            self.anchor = position
        self.previous_time = now
        self.previous_moving = moving
        return self.stalled_seconds >= self.timeout


class TurnTiming:
    """Small feedback corrections, with bounded timing variation."""
    def __init__(self, rng=None, rate=180., precise=False):
        self.rate = rate
        self.precise = precise
        self.rng = rng or random.Random()
        self.previous = None

    def observe(self, facing):
        if self.previous is not None:
            old_facing, direction, seconds = self.previous
            change = angle_error(facing, old_facing) * (1 if direction == 'right' else -1)
            # Ignore quantization/noise, delayed frames, and apparent reverse turns.
            if 4 <= change <= 60:
                measured = max(60., min(360., change/seconds))
                self.rate = .85*self.rate + .15*measured
        self.previous = None

    def duration(self, error):
        if self.precise:
            return max(.008, min(.09, abs(error)/self.rate*.65))
        nominal = max(.025, min(.09, abs(error)/self.rate*.45))
        return max(.02, min(.10, nominal*self.rng.uniform(.90, 1.10)))

    def record(self, facing, direction, seconds):
        self.previous = (facing, direction, seconds)
