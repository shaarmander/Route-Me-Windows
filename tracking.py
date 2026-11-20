"""Color/shape arrow tracking from screenshots, with explicit ambiguity rejection."""
import cv2
import numpy as np
from core import heading, distance, angle_error


def color_mask(rgb, color, tolerance):
    # RGB distance keeps calibration intuitive; lighting/UI changes require recalibration.
    delta = rgb.astype(np.float32) - np.asarray(color, dtype=np.float32)
    return (np.linalg.norm(delta, axis=2) <= tolerance).astype(np.uint8)


class ArrowTracker:
    SIZE = 61

    def __init__(self, rgb, center, tip, tolerance=65, threshold=.60, color_sample=None):
        self.threshold = threshold
        self.tolerance = tolerance
        self.last = tuple(center)
        self.lost = 0
        x, y = map(lambda v: int(round(v)), center)
        half = self.SIZE // 2
        if not (half+1 <= x < rgb.shape[1]-half-1 and half+1 <= y < rgb.shape[0]-half-1):
            raise ValueError('Player position must be at least 31 pixels inside the crop.')
        if distance(center, tip) < 4:
            raise ValueError('Click the arrow tip at least 4 pixels from its center.')
        sx, sy = map(lambda v: int(round(v)), color_sample or center)
        if distance((sx, sy), center) > 25:
            raise ValueError('Select the arrow body within 25 pixels of the position dot.')
        self.color = np.median(rgb[sy-1:sy+2, sx-1:sx+2].reshape(-1, 3), axis=0).tolist()
        patch = color_mask(rgb[y-half:y+half+1, x-half:x+half+1], self.color, tolerance)
        count, labels, stats, centers = cv2.connectedComponentsWithStats(patch, 8)
        sample_local = (sx-x+half, sy-y+half)
        candidates = [i for i in range(1, count) if 8 <= stats[i, cv2.CC_STAT_AREA] <= 900
                      and distance(centers[i], sample_local) < 18]
        if not candidates:
            raise ValueError('Could not isolate the arrow. Click inside its solid center; adjust color tolerance if needed.')
        label = min(candidates, key=lambda i: distance(centers[i], sample_local))
        base = (labels == label).astype(np.float32)
        ys, xs = np.where(base)
        if min(xs) == 0 or max(xs) == self.SIZE-1 or min(ys) == 0 or max(ys) == self.SIZE-1:
            raise ValueError('Arrow color joined the background or the arrow is too large. Change crop/calibration.')
        self.templates = []
        initial = heading(center, tip)
        for rotation in range(0, 360, 10):
            matrix = cv2.getRotationMatrix2D((half, half), -rotation, 1)
            template = cv2.warpAffine(base, matrix, (self.SIZE, self.SIZE), flags=cv2.INTER_NEAREST)
            self.templates.append(((initial + rotation) % 360, template))

    def locate(self, rgb):
        # Never silently reacquire a different arrow elsewhere on the map.
        x, y = self.last
        radius = 100
        x0, y0 = max(0, int(x)-radius), max(0, int(y)-radius)
        x1, y1 = min(rgb.shape[1], int(x)+radius), min(rgb.shape[0], int(y)+radius)
        binary = color_mask(rgb[y0:y1, x0:x1], self.color, self.tolerance).astype(np.float32)
        if min(binary.shape) < self.SIZE:
            return None
        results = []
        best = (-1, None, None)
        for direction, template in self.templates:
            scores = cv2.matchTemplate(binary, template, cv2.TM_CCOEFF_NORMED)
            _, score, _, point = cv2.minMaxLoc(scores)
            results.append((direction, scores))
            if score > best[0]:
                best = (score, point, direction)
        score, point, direction = best
        if score < self.threshold:
            return None
        # Reject shapes whose orientation cannot be distinguished (e.g. a circle).
        alternate_heading = max(float(scores[point[1], point[0]])
                                for candidate, scores in results
                                if abs(angle_error(candidate, direction)) >= 50)
        if score - alternate_heading < .035:
            return None
        # A second plausible marker at a different location makes this observation unsafe.
        runner_up = -1
        for _, scores in results:
            excluded = scores.copy()
            px, py = point
            excluded[max(0, py-12):py+13, max(0, px-12):px+13] = -1
            runner_up = max(runner_up, float(excluded.max()))
        if score - runner_up < .06:
            return None
        position = (x0+point[0]+self.SIZE//2, y0+point[1]+self.SIZE//2)
        if distance(position, self.last) > 35:
            return None
        self.last = position
        return position, direction, score
