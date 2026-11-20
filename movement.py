"""Shared movement decisions for the live controller and offline simulations."""
from dataclasses import dataclass
from collections import deque
import math
import random
import threading
import time

from core import CurveFollower, angle_error, distance, heading


class MotionHistory:
    """Only our emitted keys, timestamped even when the release timer fires.

    Prediction compensates known processing time, never supplies a missing packet
    or replaces the last verified ground observation.
    """
    def __init__(self):
        self.events=deque()
        self.before=set()
        self.since=float('-inf')
        self.lock=threading.Lock()

    def key(self, key, down, now=None):
        with self.lock:
            if len(self.events)==512:
                self.since,old_key,old_down=self.events.popleft()
                self.before.add(old_key) if old_down else self.before.discard(old_key)
            self.events.append((time.monotonic() if now is None else now,key,down))

    def predict(self, nav, world, facing, speed, rate, observed_at, now, guard=None):
        from navigation import NavigationError, MIN_STEERING_CLEARANCE
        age=now-observed_at
        if not 0<=age<=.20:
            raise NavigationError('Position processing exceeded the 200 ms prediction window.')
        with self.lock:
            events=list(self.events)
            held=set(self.before);since=self.since
        if since>observed_at:
            raise NavigationError('Movement history no longer covers this observation.')
        cuts=[]
        for when,key,down in events:
            if when<=observed_at:
                held.add(key) if down else held.discard(key)
            elif when<=now:
                cuts.append((when,key,down))
        p=tuple(world);yaw=facing;then=observed_at;travel=0.
        for until,key,down in cuts+[(now,None,False)]:
            seconds=max(0.,until-then)
            count=max(1,math.ceil(seconds/.01));dt=seconds/count
            turn=(('right' in held)-('left' in held))*rate
            side=('strafe_right' in held)-('strafe_left' in held)
            for _ in range(count):
                angle=math.radians(yaw+turn*dt/2+math.degrees(math.atan2(side,1)))
                yaw=(yaw+turn*dt)%360
                if 'forward' not in held: continue
                travel+=speed*dt
                if travel>3.:
                    raise NavigationError('Processing prediction exceeds three yards.')
                end=(p[0]+math.cos(angle)*speed*dt,p[1]-math.sin(angle)*speed*dt,p[2])
                ground=nav.engine.trace(p,end)
                if (ground is None or nav.engine.clearance(ground)<MIN_STEERING_CLEARANCE-.001 or
                    (guard is not None and not guard(end))):
                    raise NavigationError('Movement since capture has no clear predicted surface.')
                p=(*end[:2],ground[2])
            if key is not None:
                held.add(key) if down else held.discard(key)
            then=until
        return p,yaw


def comfort_width(nav, follower, world, speed):
    """Available lateral slack ahead; infinity from road-only data is not width."""
    scale=nav.transform.yards_per_pixel
    remaining=(follower.lengths[-1]-follower.progress)*scale
    width=.9
    previous=world
    for ahead in (0.,min(2.,remaining),min(max(4.,speed*.7),remaining)):
        pixel=follower.point_at(follower.progress+ahead/scale)
        requested=nav.transform.world(pixel,previous[2])
        ground=nav.engine.trace(previous,requested)
        if ground is None: return .15
        if nav.collision_checked(ground):
            width=min(width,max(.15,(nav.engine.clearance(ground)-.10)*.5))
        else:
            width=min(width,getattr(nav,'road_half_width',.4))
        previous=ground
    # Converge to the destination and tighten before bends.
    return max(.10,min(width,max(.10,remaining*.12)))


class SpeedEstimate:
    """Learn horizontal speed from verified fresh movement, independent of buffs.

    Accumulating a short interval avoids interpreting position quantization or
    a stationary turn as a new movement speed. Pauses discard the time baseline.
    """
    def __init__(self, fallback=7.):
        self.speed=fallback;self.measured=False
        self.reset()

    def reset(self):
        self.previous=None;self.elapsed=self.travel=0.

    def observe(self, world, now, moving):
        previous=self.previous
        self.previous=(tuple(world),now,moving)
        if previous is None: return self.speed
        position,then,was_moving=previous
        dt=now-then
        if not moving or not was_moving or not 0<dt<=.6:
            self.elapsed=self.travel=0.;return self.speed
        self.elapsed+=dt;self.travel+=distance(world,position)
        if self.elapsed>=.12:
            if self.travel>=.04:
                sample=self.travel/self.elapsed
                # React immediately to faster travel. A slower estimate settles
                # over a few packets, retaining conservative clearance checks.
                self.speed=sample if not self.measured else max(sample,.45*self.speed+.55*sample)
                self.measured=True
            self.elapsed=self.travel=0.
        return self.speed


class TimingNoise:
    """Bounded timing variation for maneuver durations, cooldowns and polling."""
    def __init__(self, rng=None):
        self.rng=rng or random.SystemRandom()

    def between(self, low, high):
        return self.rng.uniform(low,high)

    def scale(self, seconds, fraction=.08):
        return seconds*self.between(1-fraction,1+fraction)

    def poll(self, lease=.45, processing_seconds=0.):
        # Leave time for another capture before the checked input lease expires.
        return min(self.between(.025,.045),max(0.,lease-processing_seconds-.02))


class SteeringPolicy:
    """Heading first, with settling time between distinct corrections.

    Decisions may be computed during preview/warmup, but only applied() commits
    a maneuver. The expiry mirrors the keyboard timer, including between frames.
    Collision fallbacks can override this comfort policy and are committed too.
    """
    def __init__(self, noise=None):
        self.noise=noise or TimingNoise()
        self.active=None
        self.until=0.
        self.last_turn=None
        self.turn_ready=self.reverse_ready=self.strafe_ready=0.
        self.lateral_side=None
        self.lateral_since=0.
        self.lateral_wait=self.noise.between(.32,.48)
        self.gain=self.noise.between(.96,1.02)
        self.previous_facing=None
        self.speed_scale=1.
        self.reason='heading'

    def _end(self, now):
        if self.active in ('left','right'):
            self.last_turn=self.active
            self.turn_ready=now+self.noise.between(.18,.30)*self.speed_scale
            self.reverse_ready=now+self.noise.between(.45,.70)*self.speed_scale
            self.strafe_ready=max(self.strafe_ready,now+self.noise.between(.60,.90)*self.speed_scale)
        elif self.active and self.active.startswith('strafe_'):
            self.strafe_ready=now+self.noise.between(1.6,2.4)
            self.turn_ready=max(self.turn_ready,now+self.noise.between(.22,.36))
        self.active=None
        self.lateral_side=None

    def expire(self, now):
        if self.active and now>=self.until:
            self._end(self.until)

    def choose(self, follower, facing, error, speed, rate, walking, now):
        self.speed_scale=min(1.,7./speed)
        self.expire(now)
        repeated_heading=(self.previous_facing is not None and abs(angle_error(facing,self.previous_facing))<.01)
        self.previous_facing=facing
        scale=follower.yards_per_pixel
        offset=abs(follower.cross_track)*scale
        tangent_error=abs(angle_error(follower.tangent,facing))
        remaining=(follower.lengths[-1]-follower.progress)*scale
        comfort=getattr(follower,'comfort_width',.40)
        ahead=follower.point_at(follower.progress+6./scale)
        straight=(distance(ahead,follower.point_at(follower.progress))>.01 and
                  abs(angle_error(heading(follower.point_at(follower.progress),ahead),follower.tangent))<5.)
        side='strafe_left' if follower.cross_track>0 else 'strafe_right'
        self.reason='heading'

        # A lateral correction has one budget, rather than being renewed from
        # stale offset readings. Stop short of the centerline to avoid overshoot.
        if self.active and self.active.startswith('strafe_'):
            if side==self.active and offset>comfort and tangent_error<6 and straight:
                self.reason='lateral correction'
                return self.active,self.until-now
            self.reason='lateral settle'
            return 'forward',.1

        # Only a sustained, almost parallel offset warrants strafing. Turning
        # and strafing toward the same target doubled the correction and caused
        # the next packet to demand the opposite pair of keys.
        lateral=(walking and not self.active and now>=self.strafe_ready and
                 max(.90,comfort+.35)<=offset<=1.6 and tangent_error<=3.5 and straight and remaining>8.)
        if lateral:
            if self.lateral_side!=side:
                self.lateral_side=side;self.lateral_since=now
                self.lateral_wait=self.noise.between(.32,.48)
            if now-self.lateral_since>=self.lateral_wait:
                self.gain=self.noise.between(.94,1.02)
                self.reason='sustained parallel offset'
                return side,(offset-max(.15,comfort))/(speed/math.sqrt(2))*self.gain
            self.reason='confirm lateral drift'
            return 'forward',.1
        self.lateral_side=None

        # A small harmless offset is not a reason to steer across the centerline.
        if offset<=comfort and tangent_error<=4. and straight and remaining>3.:
            self.reason='aligned corridor'
            return 'forward',.1
        direction='right' if error>0 else 'left'
        turning=self.active in ('left','right')
        threshold=2.5 if turning and direction==self.active else (3. if not walking else 6.)
        if abs(error)<=threshold:
            self.reason='heading deadband'
            return 'forward',.1
        # Release a wrong-direction key immediately; do not hold it just to
        # satisfy a minimum duration. Small reversals wait with W still down.
        urgent=abs(error)>=30 or offset>=1.65
        reversing=(turning and direction!=self.active) or (self.last_turn and direction!=self.last_turn)
        if not urgent and ((turning and direction!=self.active) or now<self.turn_ready or
                           (reversing and now<self.reverse_ready)):
            self.reason='turn reversal settle' if reversing else 'turn settle'
            return 'forward',.1
        if self.active!=direction:
            self.gain=self.noise.between(.96,1.02)
        duration=max(.012,(abs(error)-2.5)/rate)*self.gain
        if self.active==direction and repeated_heading:
            # A fresh sequence can still repeat the previous heading. It does
            # not justify buying another full turn from the same observation.
            duration=min(duration,self.until-now)
        return ('forward_'+direction if walking and abs(error)<=55 else direction),duration

    def applied(self, command, now):
        """Record the final checked command only after input accepted it."""
        self.expire(now)
        steering=next((key for key in command.keys if key!='forward'),None)
        if steering!=self.active:
            if self.active: self._end(now)
            self.active=steering
        if steering: self.until=now+command.seconds


@dataclass(frozen=True)
class Command:
    action: str
    error: float
    seconds: float
    horizon: float
    lease: float
    reason: str='heading'
    forward_seconds: float|None=None

    @property
    def keys(self):
        if self.action=='forward': return ('forward',)
        if self.action in ('left','right'): return (self.action,)
        if self.action.startswith(('forward_','strafe_')):
            return ('forward',)+tuple(part.removeprefix('forward_') for part in self.action.split('+'))
        return ()


def recover_route(nav, follower, world):
    """Reconnect locally after drift or a blocked aim; callers release keys first."""
    # A second failed aim at the same location must not endlessly replan while
    # stationary. A yard of verified travel permits recovery at the next bend.
    anchor=getattr(follower,'target_recovery_anchor',None)
    if anchor is not None and distance(world,anchor)<1.:
        from navigation import NavigationError
        raise NavigationError('Local route recovery still has no clear steering target. Reconnect and redraw through wider ground.')
    recovered=nav.rejoin_route(world,follower.nav_route,follower.segment)
    result=CurveFollower([nav.transform.pixel(p) for p in recovered],prepared=True,
                         yards_per_pixel=nav.transform.yards_per_pixel)
    result.navigation=nav;result.nav_world=world;result.nav_route=recovered
    result.target_guard=lambda pixel:nav.target_clear(result.nav_world,pixel)
    result.tolerance=follower.tolerance
    result.target_recovery_anchor=world
    return result


def decide(nav, follower, world, position, facing, speed, turn_rate, walking, policy=None, now=None,
           motion_history=None, observed_at=None):
    if policy is None:
        if not hasattr(follower,'movement_policy'): follower.movement_policy=SteeringPolicy()
        policy=follower.movement_policy
    if now is None: now=time.monotonic()
    follower.movement_speed=speed
    follower.turn_rate=turn_rate
    nav.travel_speed=speed;nav.travel_turn_rate=turn_rate
    # Only observed positions advance route progress or establish arrival.
    action,error=follower.step(position,facing)
    if action=='arrived': return Command(action,error,.1,.16,.11,'arrived')
    follower.comfort_width=comfort_width(nav,follower,world,speed)
    predicted_world,predicted_facing=world,facing
    if motion_history is not None and observed_at is not None:
        predicted_world,predicted_facing=motion_history.predict(
            nav,world,facing,speed,turn_rate,observed_at,now,
            guard=lambda p:follower.within_corridor(nav.transform.pixel(p),1.8))
        error=angle_error(heading(nav.transform.pixel(predicted_world),follower.target),predicted_facing)
    follower.predicted_world=predicted_world
    follower.predicted_facing=predicted_facing
    follower.prediction_seconds=0. if observed_at is None else now-observed_at
    repeated=(getattr(policy,'observed_facing',None) is not None and
              abs(angle_error(facing,policy.observed_facing))<.01)
    policy.observed_facing=facing
    action,duration=policy.choose(follower,predicted_facing,error,speed,turn_rate,walking,now)
    if repeated and action.removeprefix('forward_')==policy.active:
        duration=min(duration,max(.001,policy.until-now))
    remaining=max(distance(predicted_world,follower.nav_route[-1]),
                  (follower.lengths[-1]-follower.progress)*nav.transform.yards_per_pixel-distance(world,predicted_world))
    horizon,lease=nav.movement_window(remaining,speed)
    braking=remaining/speed<.45
    forward_seconds=min(horizon-.05,remaining/speed) if braking else None
    requested=action
    corridor=max(1.8,follower.route_distance*nav.transform.yards_per_pixel+.05)
    action,duration,horizon=nav.checked_steering(predicted_world,predicted_facing,action,error,turn_rate,duration,speed,horizon,
                                              timing_gain=policy.gain,forward_seconds=forward_seconds,
                                              route_guard=lambda p:follower.within_corridor(nav.transform.pixel(p),corridor))
    lease=min(lease,horizon-.05)
    if braking or lease<.18:
        # A short clear sweep limits motion, not how long fresh feedback may
        # take while stationary. Use the same independent key-up as arrival.
        forward_seconds=min(forward_seconds if forward_seconds is not None else lease,horizon-.05)
    # A predicted pose is uncertain. The selected command must also be safe
    # from the actual observed pose; compensation never relaxes a ground check.
    if motion_history is not None and action not in ('wait','left','right') and not nav.motion_clear(world,facing,action,turn_rate,input_seconds=duration,speed=speed,
                            seconds=horizon,forward_seconds=forward_seconds,
                            route_guard=lambda p:follower.within_corridor(nav.transform.pixel(p),corridor)):
        action='wait'
    reason=policy.reason if action==requested else 'clearance override'
    turn=action.removeprefix('forward_')
    previous=policy.active if policy.active in ('left','right') else policy.last_turn
    if (action!=requested and turn in ('left','right') and previous and turn!=previous and
        abs(error)<30 and (policy.active in ('left','right') or now<policy.reverse_ready)):
        # Forward wasn't safe, so a reversal cooldown must wait stationary.
        # It must neither force W through the obstruction nor ping-pong turns.
        action='wait';reason='blocked reversal settle'
    if action=='wait' and reason!='blocked reversal settle': reason='waiting for clear ground'
    # A planned forward key-up is independent of the fresh-packet watchdog.
    # Allow capture latency after braking; no motion continues beyond its sweep.
    return Command(action,error,min(duration,lease),horizon,.45 if forward_seconds is not None else lease,reason,forward_seconds)


class JumpSchedule:
    """Occasional jumps, deferred until there is a sustained clear straight."""
    def __init__(self, now, rng=None, noise=None):
        self.noise=noise or TimingNoise(rng)
        self.next_at=now+self.noise.between(18.,40.)
        self.straight_wait=self.noise.scale(1.2)
        self.straight_since=None

    def consider(self, now, eligible):
        if not eligible:
            self.straight_since=None
            return None
        if self.straight_since is None: self.straight_since=now
        if now<self.next_at or now-self.straight_since<self.straight_wait: return None
        self.next_at=now+self.noise.between(22.,50.)
        self.straight_wait=self.noise.scale(1.2)
        self.straight_since=None
        return self.noise.between(.07,.14)
