"""Local-only scan captures and decision reports; never uploaded."""
from datetime import datetime, timezone
import json
from pathlib import Path
import uuid
import time
from collections import deque

from PIL import Image

ROOT = Path(__file__).resolve().parent / 'diagnostics'


class ScanRecord:
    def __init__(self, forced_zone=None, root=None):
        self.root = Path(root or ROOT)
        self.root.mkdir(parents=True, exist_ok=True)
        self.stem = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')+'-'+uuid.uuid4().hex[:6]
        self.path = self.root / (self.stem+'.json')
        self.data = {'captured_at_utc': None, 'started_at_utc': datetime.now(timezone.utc).isoformat(),
                     'status': 'starting', 'forced_zone': forced_zone, 'image': None}
        self.write()

    def capture(self, rgb, region):
        image_path = self.root / (self.stem+'.png')
        Image.fromarray(rgb).save(image_path)
        self.data.update(status='captured', captured_at_utc=datetime.now(timezone.utc).isoformat(),
                         image=str(image_path.resolve()), bounds=list(region),
                         image_size=[rgb.shape[1], rgb.shape[0]])
        self.write()

    def finish(self, status, message, detector=None):
        self.data.update(status=status, message=message, detector=detector or {})
        self.write()

    def write(self):
        self.path.write_text(json.dumps(self.data, indent=2), encoding='utf-8')
        (self.root/'last_scan.json').write_text(json.dumps({'report': str(self.path.resolve())}), encoding='utf-8')


def latest_report(root=None):
    root = Path(root or ROOT)
    pointer = root/'last_scan.json'
    if not pointer.exists():
        return None
    path = Path(json.loads(pointer.read_text(encoding='utf-8'))['report'])
    return path, json.loads(path.read_text(encoding='utf-8'))


def record_route_error(message, points, packet, root=None):
    root=Path(root or ROOT)
    root.mkdir(parents=True,exist_ok=True)
    report=dict(at_utc=datetime.now(timezone.utc).isoformat(),message=str(message),
                points=points,packet=packet)
    (root/'last_route_error.json').write_text(json.dumps(report,indent=2),encoding='utf-8')


class MovementRecord:
    """Bounded local evidence of controller choices and its own emitted keys."""
    def __init__(self, root=None):
        self.root=Path(root or ROOT)
        self.started=time.monotonic()
        self.frames=deque(maxlen=3000)
        self.keys=deque(maxlen=6000)

    def key(self, key, down):
        self.keys.append(dict(t=round(time.monotonic()-self.started,4),key=key,down=down))

    def frame(self, **values):
        self.frames.append(dict(t=round(time.monotonic()-self.started,4),**values))

    def finish(self, reason):
        self.root.mkdir(parents=True,exist_ok=True)
        name='movement-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')+'-'+uuid.uuid4().hex[:6]+'.json'
        path=self.root/name
        path.write_text(json.dumps(dict(reason=reason,elapsed=time.monotonic()-self.started,
                                       frames=list(self.frames),keys=list(self.keys)),indent=2),encoding='utf-8')
        (self.root/'last_movement.json').write_text(json.dumps(dict(report=str(path.resolve()))),encoding='utf-8')
