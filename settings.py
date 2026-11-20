"""Local calibration preferences, independent of saved routes."""
import json
import math
from pathlib import Path

PATH = Path(__file__).resolve().parent / 'calibration.json'
DEFAULTS = {'move_speed': .842, 'turn_rate': 180.}


def load_settings(path=PATH):
    try:
        values = json.loads(Path(path).read_text())
        result={key: float(values[key]) if math.isfinite(float(values[key])) and float(values[key])>0 else default
                for key,default in DEFAULTS.items()}
        width=float(values.get('road_half_width',.4))
        if 'road_half_width' in values and math.isfinite(width) and .1<=width<=3:
            result['road_half_width']=width
        return result
    except (OSError, ValueError, KeyError, TypeError):
        return DEFAULTS.copy()


def save_settings(speed, turn_rate, path=PATH, road_half_width=None):
    values = {'move_speed': float(speed), 'turn_rate': float(turn_rate)}
    if not all(math.isfinite(v) and v>0 for v in values.values()):
        raise ValueError('Calibration values must be positive and finite.')
    if road_half_width is not None:
        width=float(road_half_width)
        if not math.isfinite(width) or not .1<=width<=3:
            raise ValueError('Road half-width must be between 0.1 and 3 yards.')
        values['road_half_width']=width
    path = Path(path)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(values, indent=2)+'\n')
    temporary.replace(path)
