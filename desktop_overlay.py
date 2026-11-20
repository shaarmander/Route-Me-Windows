"""External Windows map overlay. The game addon supplies geometry, never drawings."""
from concurrent.futures import ThreadPoolExecutor
import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import time
import tkinter as tk

import numpy as np
from PIL import ImageGrab

from overlay_maps import clip_segment
from telemetry import decode


@dataclass(frozen=True)
class MapViewport:
    map_id: int
    canvas: tuple
    clip: tuple

    @classmethod
    def from_packet(cls, packet, game):
        view = packet.get('map_view')
        if packet.get('protocol') != 4 or not view: return None
        ox,oy,right,bottom = game
        canvas = tuple(v+(ox if i%2==0 else oy) for i,v in enumerate(view['canvas']))
        source = tuple(v+(ox if i%2==0 else oy) for i,v in enumerate(view['clip']))
        clip = (max(ox,source[0],canvas[0]),max(oy,source[1],canvas[1]),
                min(right,source[2],canvas[2]),min(bottom,source[3],canvas[3]))
        if clip[2]-clip[0]<20 or clip[3]-clip[1]<20: return None
        return cls(view['map_id'],canvas,clip)

    def normalized(self, x, y):
        if not (self.clip[0]<=x<=self.clip[2] and self.clip[1]<=y<=self.clip[3]): return None
        left,top,right,bottom = self.canvas
        return (x-left)/(right-left),(y-top)/(bottom-top)

    def line(self, segment):
        left,top,right,bottom = self.canvas
        x,y,r,b = self.clip
        w,h = r-x,b-y
        points = [((left+segment[i]*(right-left)-x)/w,
                   (top+segment[i+1]*(bottom-top)-y)/h) for i in (0,2)]
        clipped = clip_segment(*points)
        return tuple(v*(w if i%2==0 else h) for i,v in enumerate(clipped)) if clipped else None


class PanelReader:
    """One capture at a time; invalidate pending work across connection/focus changes."""
    def __init__(self, capture=None):
        self.capture = capture or self.read_panel
        self.executor = ThreadPoolExecutor(max_workers=1,thread_name_prefix='overlay-position')
        self.future = None
        self.context = None
        self.generation = 0
        self.next_read = 0.
        self.observed_at = 0.

    @staticmethod
    def read_panel(region):
        return decode(np.asarray(ImageGrab.grab(bbox=region,all_screens=True).convert('RGB')))

    def reset(self):
        if self.context is not None: self.generation += 1
        self.context = None

    def poll(self, context, region, enabled):
        now = time.monotonic()
        if not enabled: self.reset()
        elif self.context is not context:
            self.reset()
            self.context = context
        result = None
        if self.future is not None and self.future.done():
            try:
                packet = self.future.result()
                if enabled and self.capture_generation == self.generation and now-self.started < .4:
                    result = packet
                    self.observed_at = self.started
            except (OSError,ValueError):
                pass
            self.future = None
        if enabled and self.future is None and now >= self.next_read:
            self.capture_generation = self.generation
            self.started = now
            self.future = self.executor.submit(self.capture,tuple(region))
            self.next_read = now+.1
        return result

    def close(self):
        self.reset()
        self.executor.shutdown(wait=False,cancel_futures=True)


class FreshViewport:
    def __init__(self):
        self.packet = None
        self.received_at = 0.

    def update(self, packet, now=None):
        if packet and (self.packet is None or packet['sequence']!=self.packet['sequence']):
            self.packet = packet
            self.received_at = time.monotonic() if now is None else now

    def current(self, now=None):
        now = time.monotonic() if now is None else now
        return self.packet if now-self.received_at<.4 else None


class OverlayWindows:
    """Normal layered desktop windows, with no process injection or render hooks."""
    def __init__(self):
        self.api = ctypes.WinDLL('user32',use_last_error=True)
        self.api.GetAncestor.argtypes = [wintypes.HWND,wintypes.UINT]
        self.api.GetAncestor.restype = wintypes.HWND
        self.get_style = self.api.GetWindowLongPtrW if ctypes.sizeof(ctypes.c_void_p)==8 else self.api.GetWindowLongW
        self.set_style = self.api.SetWindowLongPtrW if ctypes.sizeof(ctypes.c_void_p)==8 else self.api.SetWindowLongW
        self.get_style.argtypes = [wintypes.HWND,ctypes.c_int]
        self.get_style.restype = ctypes.c_ssize_t
        self.set_style.argtypes = [wintypes.HWND,ctypes.c_int,ctypes.c_ssize_t]
        self.set_style.restype = ctypes.c_ssize_t
        self.api.SetWindowPos.argtypes = [wintypes.HWND,wintypes.HWND,ctypes.c_int,ctypes.c_int,
                                         ctypes.c_int,ctypes.c_int,wintypes.UINT]
        self.api.ShowWindow.argtypes = [wintypes.HWND,ctypes.c_int]
        self.api.CallWindowProcW.argtypes = [ctypes.c_void_p,wintypes.HWND,wintypes.UINT,wintypes.WPARAM,wintypes.LPARAM]
        self.api.CallWindowProcW.restype = ctypes.c_ssize_t
        self.procedure_type = ctypes.WINFUNCTYPE(ctypes.c_ssize_t,wintypes.HWND,wintypes.UINT,wintypes.WPARAM,wintypes.LPARAM)
        self.procedures = {}

    def handle(self, window):
        return self.api.GetAncestor(window.winfo_id(),2)

    def configure(self, window, interactive):
        handle = self.handle(window)
        if handle not in self.procedures:
            previous = self.get_style(handle,-4)
            def procedure(hwnd,message,wparam,lparam):
                if message==0x21: return 3  # WM_MOUSEACTIVATE: MA_NOACTIVATE, still deliver the click.
                return self.api.CallWindowProcW(previous,hwnd,message,wparam,lparam)
            callback = self.procedure_type(procedure)
            self.set_style(handle,-4,ctypes.cast(callback,ctypes.c_void_p).value)
            self.procedures[handle] = (previous,callback)
        style = self.get_style(handle,-20)
        style = (style | 0x08000000 | 0x00080000 | 0x80) & ~0x00040000  # NOACTIVATE, LAYERED, TOOLWINDOW
        style = style & ~0x20 if interactive else style | 0x20
        self.set_style(handle,-20,style)
        self.set_style(handle,-8,0)  # Do not disappear when the main app is minimized.

    def show(self, window, bounds):
        left,top,right,bottom = map(round,bounds)
        handle = self.handle(window)
        if not self.api.SetWindowPos(handle,-1,left,top,right-left,bottom-top,0x10|0x40):
            raise ctypes.WinError(ctypes.get_last_error())  # NOACTIVATE | SHOWWINDOW

    def hide(self, window):
        self.api.ShowWindow(self.handle(window),0)

    def detach(self, window):
        handle = self.handle(window)
        previous = self.procedures.pop(handle,None)
        if previous: self.set_style(handle,-4,previous[0])


class RoadOverlay:
    COLORKEY = '#010203'

    def __init__(self, root, on_destination, windows=None):
        self.window = tk.Toplevel(root)
        self.window.withdraw()
        self.window.overrideredirect(True)
        self.window.attributes('-transparentcolor',self.COLORKEY)
        self.window.configure(bg=self.COLORKEY)
        self.canvas = tk.Canvas(self.window,bg=self.COLORKEY,highlightthickness=0)
        self.canvas.pack(fill='both',expand=True)
        self.window.update_idletasks()
        self.windows = windows or OverlayWindows()
        self.windows.configure(self.window,False)
        self.on_destination = on_destination
        self.view = None
        self.interactive = False
        self.press = None
        self.last_frame = None
        self.observed_at = 0.
        self.canvas.bind('<ButtonPress-1>',self.down)
        self.canvas.bind('<ButtonRelease-1>',self.up)

    def hide(self):
        self.windows.hide(self.window)
        self.view = None
        self.press = None
        self.last_frame = None

    def show(self, view, segments, route=(), interactive=False, status='', observed_at=None):
        if view != self.view: self.press = None
        self.view = view
        self.observed_at = time.monotonic() if observed_at is None else observed_at
        if interactive != self.interactive:
            self.interactive = interactive
            self.press = None
            # A faint selection surface receives clicks only while Alt is held.
            self.window.attributes('-transparentcolor','' if interactive else self.COLORKEY)
            self.window.attributes('-alpha',.55 if interactive else 1.)
            self.canvas.configure(bg='#17212b' if interactive else self.COLORKEY,
                                  cursor='crosshair' if interactive else '')
            self.windows.configure(self.window,interactive)
        frame = (view,id(segments),id(route),interactive,status)
        if frame != self.last_frame:
            self.canvas.delete('all')
            for strokes,color,width in ((segments,'#35d8ff',2),(route,'#ffcf56',3)):
                for segment in strokes:
                    points = view.line(segment)
                    if points: self.canvas.create_line(*points,fill=color,width=width)
            self.canvas.create_text(9,9,anchor='nw',text=status[:170],fill='#ffffff',
                                    font=('Segoe UI',10,'bold'),width=max(80,view.clip[2]-view.clip[0]-18))
            self.last_frame = frame
        self.windows.show(self.window,view.clip)

    def down(self, event):
        if self.interactive and self.view and time.monotonic()-self.observed_at<.4:
            self.press = (self.view,event.x_root,event.y_root)

    def up(self, event):
        press,self.press = self.press,None
        if not press or not self.interactive or self.view!=press[0] or time.monotonic()-self.observed_at>=.4:
            return
        if (event.x_root-press[1])**2+(event.y_root-press[2])**2>16: return
        point = self.view.normalized(event.x_root,event.y_root)
        if point: self.on_destination(self.view,point)

    def close(self):
        self.windows.detach(self.window)
        self.window.destroy()
