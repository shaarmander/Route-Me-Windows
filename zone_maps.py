"""Cached Classic maps and screenshot-only zone/map/arrow acquisition."""
from dataclasses import dataclass
from difflib import SequenceMatcher
import json
import math
from pathlib import Path
import re

import cv2
import numpy as np
from PIL import Image

from core import angle_error, distance
from tracking import ArrowTracker, color_mask

ASSETS = Path(__file__).resolve().parent / 'assets'


class AcquisitionError(ValueError):
    pass


class MapCatalog:
    def __init__(self, root=None):
        self.root = Path(root or ASSETS / 'maps')
        profile=self.root/'profile.json'
        self.profile=json.loads(profile.read_text(encoding='utf-8')) if profile.exists() else None
        if self.profile is not None and (type(self.profile.get('version')) is not int or self.profile.get('version')!=1 or not isinstance(self.profile.get('zones'),dict)):
            raise ValueError('Unsupported client map profile.')
        self.zones = json.loads((self.root / 'catalog.json').read_text(encoding='utf-8'))
        self.by_id = {zone['id']: zone for zone in self.zones}

    def require_navigation(self, zone_id, packet=None):
        """Validate current-map identity and return whether to use legacy collision."""
        if not self.profile:
            return True
        from navigation import NavigationError
        profile=self.profile
        # Check the build even in legacy-covered zones, before a journey can
        # cross into an updated zone with incompatible maps.
        if packet is not None and packet.get('build')!=profile['build']:
            raise NavigationError(
                f"Live client build {packet.get('build', 'unknown')} differs from selected maps "
                f"{profile['client_version']} (build {profile['build']}). "
                f"Select matching maps in the Maps dropdown; if unavailable, extract a new profile. Movement withheld.")
        zone=profile['zones'].get(str(zone_id))
        if zone is None:
            return True
        if packet is not None and packet.get('map_id')!=zone['ui_map_id']:
            raise NavigationError(
                f"Live UI map {packet.get('map_id', 'unknown')} differs from "
                f"{self.by_id[zone_id]['name']} (expected UI map {zone['ui_map_id']}). "
                "Reconnect in the correct zone. Movement withheld.")
        # Updated maps follow saved roads without substituting the old mesh.
        # A metadata flag cannot turn the legacy geometry into current geometry.
        return False

    def image(self, zone_id):
        return np.asarray(Image.open(self.root / self.by_id[zone_id]['file']).convert('RGB')).copy()

    def match_name(self, text):
        normal = lambda value: re.sub('[^a-z]', '', value.lower()).removeprefix('the')
        text = normal(text)
        if len(text) < 5:
            return None
        scores = sorted(((SequenceMatcher(None, text, normal(z['name'])).ratio(), z)
                         for z in self.zones), key=lambda item: item[0], reverse=True)
        if scores[0][0] < .88 or scores[0][0]-scores[1][0] < .08:
            return None
        return scores[0][1]


def register_map(rgb, reference):
    """Fit north-up map artwork to a game screenshot, tolerating unexplored areas.

    The shared frame can provide alignment, but MUST NOT establish zone identity.
    """
    factor = min(1, 1900/max(rgb.shape[:2]))
    small = cv2.resize(rgb, None, fx=factor, fy=factor) if factor < 1 else rgb
    sift = cv2.SIFT_create(nfeatures=5000)
    keys, desc = sift.detectAndCompute(cv2.cvtColor(small, cv2.COLOR_RGB2GRAY), None)
    refkeys, refdesc = sift.detectAndCompute(cv2.cvtColor(reference, cv2.COLOR_RGB2GRAY), None)
    if desc is None or refdesc is None:
        raise AcquisitionError('Could not find map artwork. Open the full zone map.')
    pairs = cv2.BFMatcher().knnMatch(refdesc, desc, k=2)
    matches = [pair[0] for pair in pairs if len(pair)==2 and pair[0].distance < .70*pair[1].distance]
    if len(matches) < 12:
        raise AcquisitionError('Not enough matching map detail. Keep the whole zone map visible.')
    source = np.float32([refkeys[m.queryIdx].pt for m in matches])
    destination = np.float32([keys[m.trainIdx].pt for m in matches]) / factor
    matrix, inliers = cv2.estimateAffinePartial2D(source, destination, method=cv2.RANSAC,
                                                ransacReprojThreshold=4/factor)
    if matrix is None or inliers is None or int(inliers.sum()) < 12:
        raise AcquisitionError('Map alignment is uncertain; no route has been activated.')
    selected = source[inliers.ravel().astype(bool)]
    height, width = reference.shape[:2]
    span = np.ptp(selected, axis=0)
    if span[0] < width*.45 or span[1] < height*.45:
        raise AcquisitionError('Matching detail is too localized to align the full map.')
    sx, sy = matrix[0, 0], matrix[1, 1]
    if not (.25 < sx < 6 and .25 < sy < 6) or abs(matrix[1, 0])/sx > .015:
        raise AcquisitionError('Expected a north-up, unrotated Classic zone map.')
    left, top = matrix[:, 2]
    right, bottom = left+sx*width, top+sy*height
    sh, sw = rgb.shape[:2]
    if left < -.06*(right-left) or top < -.06*(bottom-top) or right > sw+.06*(right-left) or bottom > sh+.06*(bottom-top):
        raise AcquisitionError('The map is cropped too far. Keep the full zone visible.')
    return tuple(map(float, (left, top, right, bottom)))


def label_belongs_to_map(box, bounds):
    x, y = np.mean(box, axis=0)
    left, top, right, bottom = bounds
    nx, ny = (x-left)/(right-left), (y-top)/(bottom-top)
    # Exclude quest/zone sidebars and adjacent-zone labels in the map interior.
    return 0 <= nx <= 1 and ((.86 <= ny <= .98) or (-.14 <= ny <= .055))


def detect_arrow(rgb, bounds):
    """Acquire the supplied Classic marker style without user clicks.

    Scan several UI scales and orientations, then require a distinctive match.
    Runtime tracking uses the matched silhouette around its calibrated pivot.
    """
    seed_rgb = np.asarray(Image.open(ASSETS/'classic_arrow.png').convert('RGB'))
    # The small blue decoration is offset from the map-coordinate anchor. The
    # provided Player readout establishes the seed sprite's approximate anchor.
    seed = ArrowTracker(seed_rgb, (49, 49), (63, 54), color_sample=(47, 43))
    left, top, right, bottom = bounds
    h, w = rgb.shape[:2]
    x0, y0 = max(0, int(left)), max(0, int(top))
    x1, y1 = min(w, int(right)), min(h, int(bottom))
    # The dot/body palette is independent of fog artwork. Work at roughly the
    # source screenshot scale to bound the global acquisition cost.
    shrink = min(1, 1000/(x1-x0))
    crop = cv2.resize(rgb[y0:y1, x0:x1], None, fx=shrink, fy=shrink)
    binary = color_mask(crop, seed.color, seed.tolerance).astype(np.float32)
    candidates = []
    for scale in (.60, .75, .90, 1., 1.15, 1.35, 1.60):
        effective = scale*shrink
        size = max(15, round(seed.SIZE*effective))
        if size % 2 == 0:
            size += 1
        for facing, template in seed.templates:
            resized = cv2.resize(template, (size, size), interpolation=cv2.INTER_NEAREST)
            result = cv2.matchTemplate(binary, resized, cv2.TM_CCOEFF_NORMED)
            for _ in range(2):
                _, score, _, point = cv2.minMaxLoc(result)
                px, py = (point[0]+size//2)/shrink+x0, (point[1]+size//2)/shrink+y0
                candidates.append((score, (px, py), facing, size/shrink))
                radius = max(10, size//3)
                result[max(0, point[1]-radius):point[1]+radius+1,
                       max(0, point[0]-radius):point[0]+radius+1] = -1
    candidates.sort(key=lambda item: item[0], reverse=True)
    best = candidates[0]
    score, position, facing, matched_size = best
    other = max((item[0] for item in candidates if distance(position, item[1]) > 16), default=-1)
    if score < .68 or score-other < .06:
        raise AcquisitionError('Player arrow is not distinctive enough. Use Calibrate arrow once for your marker style.')
    # Build the runtime tracker from the actual screenshot, rotating the known
    # color-sample offset about the detected position dot.
    delta = math.radians(angle_error(facing, seed.templates[0][0]))
    scale = matched_size/seed.SIZE
    dx, dy = -2*scale, -6*scale
    sample = (position[0]+dx*math.cos(delta)-dy*math.sin(delta),
              position[1]+dx*math.sin(delta)+dy*math.cos(delta))
    length = math.hypot(14, 5)*scale
    tip = (position[0]+length*math.sin(math.radians(facing)),
           position[1]-length*math.cos(math.radians(facing)))
    tracker = ArrowTracker(rgb, position, tip, color_sample=sample)
    found = tracker.locate(rgb)
    if found is None:
        raise AcquisitionError('Player arrow was found but could not be verified. Calibrate arrow once.')
    return position, sample, tip, found


@dataclass
class Acquisition:
    zone: dict
    image: np.ndarray
    bounds: tuple
    title_bounds: tuple | None
    arrow: tuple | None
    warning: str = ''
    coordinate_anchor: tuple | None = None


class ZoneDetector:
    def __init__(self, catalog=None):
        self.catalog = catalog or MapCatalog()
        self.ocr = None
        self.diagnostics = {}

    def detect(self, rgb, forced_zone=None):
        self.diagnostics = {'ocr': [], 'candidates': [], 'forced_zone': forced_zone}
        results = []
        if forced_zone is not None:
            zone = self.catalog.by_id[forced_zone]
            reference = self.catalog.image(forced_zone)
            acquisition = Acquisition(zone, reference, register_map(rgb, reference), None, None)
        else:
            if self.ocr is None:
                from rapidocr_onnxruntime import RapidOCR
                self.ocr = RapidOCR(intra_op_num_threads=2, inter_op_num_threads=2)
            results, _ = self.ocr(cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR))
            self.diagnostics['ocr'] = [dict(text=text, confidence=float(score), box=np.asarray(box).tolist())
                                       for box, text, score in results or []]
            possible = []
            for box, text, confidence in results or []:
                if confidence < .70:
                    continue
                zone = self.catalog.match_name(text)
                if zone is None:
                    continue
                candidate = {'zone': zone['name'], 'id': zone['id'], 'text': text}
                self.diagnostics['candidates'].append(candidate)
                reference = self.catalog.image(zone['id'])
                try:
                    bounds = register_map(rgb, reference)
                except AcquisitionError as error:
                    candidate['rejected'] = str(error)
                    continue
                candidate['map_bounds'] = list(bounds)
                if not label_belongs_to_map(box, bounds):
                    candidate['rejected'] = 'Label outside the map title/header area (for example a sidebar entry).'
                    continue
                candidate['accepted'] = True
                corners = np.array(box)
                x0, y0 = np.floor(corners.min(axis=0)).astype(int)
                x1, y1 = np.ceil(corners.max(axis=0)).astype(int)
                possible.append(Acquisition(zone, reference, bounds, (int(x0),int(y0),int(x1),int(y1)), None))
            if len({p.zone['id'] for p in possible}) != 1:
                raise AcquisitionError('Could not identify one current zone title. Keep its name visible, or choose the zone from the list.')
            acquisition = possible[0]
        try:
            acquisition.arrow = detect_arrow(rgb, acquisition.bounds)
            # Optional one-time anchor calibration from an ALREADY VISIBLE label.
            # The live movement loop continues to use pixels of the arrow only.
            for box, text, confidence in results or []:
                match = re.fullmatch(r'(?:player|plaver)\s*:\s*(\d{1,3}\.\d+)\s*,\s*(\d{1,3}\.\d+)', text.strip(), re.I)
                if not match or confidence < .85:
                    continue
                coordinates = tuple(map(float, match.groups()))
                if not all(0 <= value <= 100 for value in coordinates):
                    continue
                left, top, right, bottom = acquisition.bounds
                bx, by = np.mean(box, axis=0)
                if not (left <= bx <= right and top+.90*(bottom-top) <= by <= bottom+30):
                    continue
                anchor = (left+coordinates[0]/100*(right-left), top+coordinates[1]/100*(bottom-top))
                position, sample, tip, found = acquisition.arrow
                if distance(anchor, position) > 16 or distance(anchor, tip) < 5:
                    continue
                tracker = ArrowTracker(rgb, anchor, tip, color_sample=sample)
                verified = tracker.locate(rgb)
                if verified:
                    acquisition.arrow = (anchor, sample, tip, verified)
                    acquisition.coordinate_anchor = coordinates
        except (AcquisitionError, ValueError) as error:
            acquisition.warning = str(error)
        self.diagnostics['selected_zone'] = acquisition.zone['name']
        self.diagnostics['map_bounds'] = list(acquisition.bounds)
        self.diagnostics['warning'] = acquisition.warning
        if acquisition.arrow:
            self.diagnostics['arrow_position'] = list(acquisition.arrow[0])
            self.diagnostics['arrow_confidence'] = float(acquisition.arrow[3][2])
        return acquisition


def title_unchanged(reference, live, title_bounds):
    if title_bounds is None:
        return True
    x0, y0, x1, y1 = title_bounds
    a = cv2.cvtColor(reference[y0:y1, x0:x1], cv2.COLOR_RGB2GRAY)
    b = cv2.cvtColor(live[y0:y1, x0:x1], cv2.COLOR_RGB2GRAY)
    if a.shape != b.shape or a.size == 0 or a.std() < 3:
        return False
    return float(cv2.matchTemplate(b, a, cv2.TM_CCOEFF_NORMED)[0, 0]) >= .86
