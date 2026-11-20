"""Binary addon panel decoder. No OCR, arrow matching, or game memory reads."""
import time
import numpy as np
import cv2

WIDTH, HEIGHT = 256, 36
COLORS = np.array([(255,0,255),(0,255,255),(255,255,0),(0,0,255)], dtype=np.uint8)


def _decode(rgb, count):
    height, width = rgb.shape[:2]
    if width < 128 or height < 18:
        raise ValueError('Addon panel is too small to read. Connect again.')
    columns = np.floor((np.arange(count)+.5)*width/count).astype(int)
    rows = np.floor((np.arange(9)+.5)*height/9).astype(int)
    header = rgb[rows[0], columns].astype(int)
    if np.max(np.abs(header-np.tile(COLORS, (count//4,1)).astype(int))) > 65:
        raise ValueError('Addon panel is hidden or moved.')
    samples = rgb[rows[1:,None], columns[None,:]].astype(int)
    white = np.all(samples > 190, axis=2)
    black = np.all(samples < 65, axis=2)
    if not np.all(white | black):
        raise ValueError('Addon panel pixels are obscured or distorted.')
    data = np.sum(white.astype(int)*(2**np.arange(8))[:,None], axis=0).tolist()
    if data[:2] != [82,77] or data[2] not in ((4,) if count==80 else (1,2,3)) or sum(data[:-2])%251 != data[-2] or sum((i+1)*v for i,v in enumerate(data[:-2]))%251 != data[-1]:
        raise ValueError('Addon packet failed integrity checks.')
    if not data[3]&1:
        raise ValueError('Addon cannot read position/facing on this client or in this location.')
    u16 = lambda i: data[i]+256*data[i+1]
    if not u16(6) or u16(12)>=36000:
        raise ValueError('Invalid zone or heading from addon.')
    packet = dict(sequence=u16(4), map_id=u16(6), x=u16(8)/65535, y=u16(10)/65535,
                facing=u16(12)/100, stealth=bool(data[3]&2),
                name=bytes(data[16:40 if data[2]>=2 else 56]).split(b'\0')[0].decode('utf-8'),
                protocol=data[2], world=None, continent=None, build=None, world_z_valid=False)
    if data[2]>=2 and data[3]&4:
        packet['world']=tuple(int.from_bytes(bytes(data[i:i+4]),'little',signed=True)/100 for i in (40,44)) + (
            int.from_bytes(bytes(data[48:52]),'little',signed=True)/100 if data[2]!=3 else 0.,)
        packet['continent']=u16(52)
        packet['build']=int.from_bytes(bytes(data[54:58]),'little')
        if any(abs(v)>100000 for v in packet['world']):
            raise ValueError('Invalid world position from addon.')
    if data[2]==4:
        packet['map_view'] = None
        if data[3]&8:
            signed = lambda i: int.from_bytes(bytes(data[i:i+2]),'little',signed=True)
            canvas = tuple(signed(i) for i in (60,62,64,66))
            clip = tuple(signed(i) for i in (68,70,72,74))
            if not u16(58) or any(r[2]<=r[0] or r[3]<=r[1] for r in (canvas,clip)):
                raise ValueError('Invalid map viewport from addon.')
            packet['map_view'] = dict(map_id=u16(58),canvas=canvas,clip=clip)
    return packet


def decode(rgb):
    count = 80 if rgb.shape[1]/max(1,rgb.shape[0])>8 else 64
    return _decode(rgb,count)


def find_panel(rgb):
    # Infer cell pitch from the repeating header, supporting 64 or 80 columns.
    mask = ((rgb[:,:,0]>190)&(rgb[:,:,1]<65)&(rgb[:,:,2]>190)).astype(np.uint8)
    count, _, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
    cells = [tuple(map(int,s)) for s in stats[1:] if 2<=s[2]<=20 and 2<=s[3]<=20 and s[4]>=.6*s[2]*s[3]]
    detail = None
    for x,y,w,h,area in cells:
        aligned = sorted([c for c in cells if c[0]>=x and abs(c[1]-y)<=1 and abs(c[3]-h)<=1])
        if len(aligned)<16:
            continue
        positions = np.array([c[0] for c in aligned[:16]])
        pitch = (positions[-1]-positions[0])/15
        if pitch<8 or pitch>80 or np.max(np.abs(np.diff(positions)-pitch))>2:
            continue
        for count in (80,64):
            if len(aligned)<count//4: continue
            estimate = round(pitch*count/4)
            widths = [estimate+d for d in (0,-1,1,-2,2)]
            for width in widths:
                heights = list(dict.fromkeys([round(width*9/count)+d for d in (0,-1,1,-2,2)]+[h*9]))
                for height in heights:
                    for dx,dy in ((0,0),(-1,0),(0,-1),(-1,-1),(1,0),(0,1)):
                        left,top=x+dx,y+dy
                        if left<0 or top<0 or left+width>rgb.shape[1] or top+height>rgb.shape[0]:
                            continue
                        try:
                            packet = _decode(rgb[top:top+height,left:left+width],count)
                            return (left,top,left+width,top+height), packet
                        except ValueError as error:
                            if 'cannot read position' in str(error):
                                raise
                            if 'integrity' in str(error) or 'obscured' in str(error):
                                detail = str(error)
    if detail:
        raise ValueError('Addon header found, but data could not be decoded: '+detail)
    raise ValueError('Wayfinder panel not found. Enable Wayfinder, log in, and type /wayfinder. Try /wayfinder large if needed.')


class AddonTracker:
    def __init__(self, map_id, shape, center):
        self.map_id = map_id
        self.height, self.width = shape[:2]
        self.last = tuple(center)
        self.sequence = None
        self.changed = time.monotonic()
        self.packet = None

    def locate(self, rgb):
        packet = decode(rgb)
        if packet['map_id'] != self.map_id:
            raise ValueError('Zone changed. Connect again and draw a route in the new zone.')
        now = time.monotonic()
        if packet['sequence'] != self.sequence:
            self.sequence, self.changed = packet['sequence'], now
        elif now-self.changed > .75:
            raise ValueError('Addon data stopped updating.')
        self.packet = packet
        self.last = (packet['x']*self.width, packet['y']*self.height)
        return self.last, packet['facing'], 1.
