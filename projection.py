"""Registration of two north-up maps of the same Classic zone.

Both rectangles describe the full zone's 0..100 extent, excluding browser/game
chrome. Steering stays in live-map pixels; reference image scale cannot affect it.
"""
import math
from dataclasses import dataclass
from core import heading


def validate_bounds(bounds):
    if len(bounds) != 4 or not all(math.isfinite(v) for v in bounds):
        raise ValueError('Map bounds must contain four finite coordinates.')
    if bounds[2]-bounds[0] < 30 or bounds[3]-bounds[1] < 30:
        raise ValueError('Select the complete map extent, at least 30 pixels across.')
    return tuple(bounds)


@dataclass(frozen=True)
class MapProjection:
    live: tuple
    plan: tuple

    def __post_init__(self):
        validate_bounds(self.live)
        validate_bounds(self.plan)

    @staticmethod
    def transform(point, source, destination):
        x, y = point
        l, t, r, b = source
        dl, dt, dr, db = destination
        return (dl+(x-l)/(r-l)*(dr-dl), dt+(y-t)/(b-t)*(db-dt))

    def to_plan(self, point):
        return self.transform(point, self.live, self.plan)

    def to_live(self, point):
        return self.transform(point, self.plan, self.live)

    def zone_coordinates(self, point):
        return self.transform(point, self.live, (0, 0, 100, 100))

    def plan_facing(self, facing):
        # Directions are vectors: translation must not affect them, and different
        # x/y image scaling must be accounted for when drawing the heading overlay.
        vector = (math.sin(math.radians(facing)), -math.cos(math.radians(facing)))
        origin = self.to_plan((0, 0))
        return heading(origin, self.to_plan(vector))

