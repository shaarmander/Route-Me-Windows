"""Normal Windows SendInput keystrokes, no process/game-memory access."""
import ctypes
import math
from pathlib import PureWindowsPath
import re
from ctypes import wintypes
import time
import threading

user32 = ctypes.WinDLL('user32', use_last_error=True)
kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
ULONG_PTR = wintypes.WPARAM


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [('dx', wintypes.LONG), ('dy', wintypes.LONG), ('mouseData', wintypes.DWORD),
                ('dwFlags', wintypes.DWORD), ('time', wintypes.DWORD), ('dwExtraInfo', ULONG_PTR)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [('wVk', wintypes.WORD), ('wScan', wintypes.WORD), ('dwFlags', wintypes.DWORD),
                ('time', wintypes.DWORD), ('dwExtraInfo', ULONG_PTR)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [('uMsg', wintypes.DWORD), ('wParamL', wintypes.WORD), ('wParamH', wintypes.WORD)]


class PAYLOAD(ctypes.Union):
    _fields_ = [('ki', KEYBDINPUT), ('mi', MOUSEINPUT), ('hi', HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ('payload',)
    _fields_ = [('type', wintypes.DWORD), ('payload', PAYLOAD)]


user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
user32.SendInput.restype = wintypes.UINT
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short
user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
ENUM_WINDOWS_PROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
user32.EnumWindows.argtypes = [ENUM_WINDOWS_PROC, wintypes.LPARAM]
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsIconic.argtypes = [wintypes.HWND]
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
user32.GetWindow.restype = wintypes.HWND
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL
GAME_EXECUTABLE = re.compile(r'wow(?:classic)?(?:b|t)?(?:-64)?\.exe',re.IGNORECASE)


def window_process_name(handle):
    """Read the owning executable's name, without accessing process memory."""
    pid=wintypes.DWORD()
    if not user32.GetWindowThreadProcessId(handle,ctypes.byref(pid)) or not pid.value:
        return None
    process=kernel32.OpenProcess(0x1000,False,pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not process: return None
    try:
        path=ctypes.create_unicode_buffer(32768)
        size=wintypes.DWORD(len(path))
        if not kernel32.QueryFullProcessImageNameW(process,0,path,ctypes.byref(size)):
            return None
        return PureWindowsPath(path.value).name
    finally:
        kernel32.CloseHandle(process)


def is_game_window(handle):
    if not handle or not user32.IsWindowVisible(handle) or user32.GetWindow(handle,4):  # GW_OWNER
        return False
    title=ctypes.create_unicode_buffer(512)
    user32.GetWindowTextW(handle,title,len(title))
    if 'world of warcraft' not in title.value.casefold(): return False
    executable=window_process_name(handle)
    return bool(executable and GAME_EXECUTABLE.fullmatch(executable))


def game_windows():
    windows=[]
    @ENUM_WINDOWS_PROC
    def visit(handle,_):
        if is_game_window(handle): windows.append(handle)
        return True
    user32.EnumWindows(visit,0)
    return windows


def focus_game():
    """Called once from the user's click; never refocus during a running route."""
    if game_focused():
        return
    windows = game_windows()
    if not windows:
        raise ValueError('Open World of Warcraft and log in first.')
    if len(windows)>1:
        raise ValueError('Multiple WoW windows are open. Keep only the intended game window open.')
    handle = windows[0]
    if user32.IsIconic(handle):
        user32.ShowWindow(handle, 9)  # Restore a minimized game without toggling keys.
    user32.SetForegroundWindow(handle)
    for _ in range(10):
        if game_focused():
            return
        time.sleep(.02)
    raise ValueError('Windows blocked automatic focus. Bring WoW forward once, then retry.')


def game_focused():
    return is_game_window(user32.GetForegroundWindow())


def escape_pressed():
    return bool(user32.GetAsyncKeyState(0x1B) & 0x8000)


def stop_hotkey_pressed():
    return escape_pressed() or bool(user32.GetAsyncKeyState(0x08) & 0x8000)


def overlay_select_pressed():
    return bool(user32.GetAsyncKeyState(0x12) & 0x8000) and not any(
        user32.GetAsyncKeyState(key) & 0x8000 for key in (0x10,0x11))


def input_modifiers_held():
    return any(user32.GetAsyncKeyState(key) & 0x8000 for key in (0x01,0x10,0x11,0x12))


def foreground_game_region():
    if not game_focused():
        raise ValueError('Switch to World of Warcraft with the full zone map open during the countdown.')
    handle = user32.GetForegroundWindow()
    rectangle = wintypes.RECT()
    origin = wintypes.POINT(0, 0)
    if not user32.GetClientRect(handle, ctypes.byref(rectangle)) or not user32.ClientToScreen(handle, ctypes.byref(origin)):
        raise ctypes.WinError(ctypes.get_last_error())
    return (origin.x, origin.y, origin.x+rectangle.right, origin.y+rectangle.bottom)


class Keyboard:
    KEYS = {'forward': (0x11, False), 'left': (0x4B, True), 'right': (0x4D, True),
            'strafe_left': (0x10, False), 'strafe_right': (0x12, False), 'jump': (0x39, False)}

    def __init__(self):
        self.held = set()
        self._lock = threading.RLock()
        self._deadline = None
        self._steering_deadline = None
        self._forward_deadline = None
        self._jump_deadline = None
        self._guard_exit = threading.Event()
        self._guard_thread = None
        self.stop_reason = ''
        self.last_duration = 0.
        self.pause_on_focus_loss = False
        self.focus_paused = threading.Event()
        self.event_sink = None

    def send(self, key, down):
        with self._lock:
            self._send(key, down)

    def _send(self, key, down):
        scan, extended = self.KEYS[key]
        flags = 0x0008 | (0x0001 if extended else 0) | (0 if down else 0x0002)
        event = INPUT(type=1, ki=KEYBDINPUT(0, scan, flags, 0, 0))
        if user32.SendInput(1, ctypes.byref(event), ctypes.sizeof(INPUT)) != 1:
            raise ctypes.WinError(ctypes.get_last_error())
        if down:
            self.held.add(key)
        else:
            self.held.discard(key)
        if self.event_sink is not None:
            self.event_sink(key,down)

    def release(self):
        with self._lock:
            self._deadline = None
            self._steering_deadline = None
            self._forward_deadline = None
            self._jump_deadline = None
            for key in list(self.held):
                self._send(key, False)

    def is_held(self, key):
        with self._lock:
            return key in self.held

    def _check_guard(self, stop):
        """Independent of screenshot processing; key hold expires without feedback."""
        with self._lock:
            reason = ''
            if stop.is_set():
                reason = 'Stopped.'
            elif stop_hotkey_pressed():
                reason = 'Stopped with Backspace or Esc.'
            elif not game_focused():
                if self.pause_on_focus_loss:
                    self.focus_paused.set()
                    self.release()
                    return False
                reason = 'Stopped: World of Warcraft lost foreground focus.'
            elif self.focus_paused.is_set():
                return False  # The worker must validate fresh telemetry before resuming.
            elif self._deadline is not None and time.monotonic() >= self._deadline:
                reason = 'Stopped: tracking updates stalled; movement keys released.'
            if reason:
                self.stop_reason = reason
                stop.set()
                self.release()
                return False
            if self._steering_deadline is not None and time.monotonic()>=self._steering_deadline:
                for key in list(self.held-{'forward','jump'}):
                    self._send(key,False)
                self._steering_deadline=None
            if self._forward_deadline is not None and time.monotonic()>=self._forward_deadline:
                for key in list(self.held&{'forward','strafe_left','strafe_right'}): self._send(key,False)
                self._forward_deadline=None
            if self._jump_deadline is not None and time.monotonic()>=self._jump_deadline:
                if 'jump' in self.held: self._send('jump',False)
                self._jump_deadline=None
            return True

    def start_guard(self, stop):
        if self._guard_thread is not None:
            return
        def watch():
            try:
                while not self._guard_exit.wait(.01):
                    if not self._check_guard(stop):
                        if self.focus_paused.is_set() and not stop.is_set():
                            continue
                        return
            except Exception as error:
                self.stop_reason = f'Stopped: keyboard guard failed: {error}'
                stop.set()
                try:
                    self.release()
                except Exception:
                    pass
        self._guard_thread = threading.Thread(target=watch, daemon=True)
        self._guard_thread.start()

    def hold_forward(self, stop, lease_seconds=None):
        """Refresh the feedback deadline without toggling an already-held W key."""
        self.start_guard(stop)
        with self._lock:
            if not self._check_guard(stop):
                return False
            for key in list(self.held - {'forward','jump'}):
                self._send(key, False)
            self._steering_deadline=None
            self._forward_deadline=None
            self._deadline = time.monotonic()+(lease_seconds if lease_seconds is not None else getattr(self,'feedback_lease',1.5))
            if 'forward' not in self.held:
                self._send('forward', True)
            return True

    def steer(self, keys, seconds, stop):
        """Refresh compatible steering without blocking capture or re-pressing keys."""
        return self.command(set(keys)|{'forward'},seconds,stop)

    def command(self, keys, seconds, stop, forward_seconds=None):
        """Apply a persistent key set, including a complete stationary turn."""
        selected=set(keys)
        if not selected or not selected <= {'forward','left','right','strafe_left','strafe_right'} or {'left','right'}<=selected or {'strafe_left','strafe_right'}<=selected:
            raise ValueError('Steering requires compatible turn and strafe keys.')
        if not math.isfinite(seconds) or seconds<=0:
            raise ValueError('Steering duration must be positive and finite.')
        if forward_seconds is not None and (not math.isfinite(forward_seconds) or forward_seconds<=0):
            raise ValueError('Forward duration must be positive and finite.')
        self.start_guard(stop)
        with self._lock:
            if not self._check_guard(stop): return False
            desired=selected|({'jump'} if 'jump' in self.held else set())
            for key in sorted(self.held-desired): self._send(key,False)
            for key in sorted(desired-self.held): self._send(key,True)
            now=time.monotonic()
            self._deadline=now+getattr(self,'feedback_lease',1.5)
            self._forward_deadline=(now+min(forward_seconds,getattr(self,'feedback_lease',1.5))
                                    if forward_seconds is not None and 'forward' in selected else None)
            self._steering_deadline=(now+min(seconds,getattr(self,'feedback_lease',1.5))
                                     if selected-{'forward'} else None)
            return True

    def jump(self, seconds, stop):
        if not math.isfinite(seconds) or not .03<=seconds<=.2:
            raise ValueError('Jump press duration is out of range.')
        with self._lock:
            if not self._check_guard(stop) or self.held!={'forward'}: return False
            self._send('jump',True)
            self._jump_deadline=time.monotonic()+seconds
            return True

    def close(self):
        self._guard_exit.set()
        try:
            self.release()
        finally:
            if self._guard_thread is not None:
                self._guard_thread.join(timeout=.25)

    def nudge(self, key, seconds, stop):
        """Keep W down while compatible turn and strafe keys overlap."""
        keys = (key,) if isinstance(key,str) else tuple(dict.fromkeys(key))
        selected = set(keys)
        if not selected or not selected <= {'left','right','strafe_left','strafe_right'} or {'left','right'}<=selected or {'strafe_left','strafe_right'}<=selected:
            raise ValueError('Steering requires compatible turn and strafe keys.')
        completed = False
        started = None
        try:
            if not self.hold_forward(stop):
                return False
            with self._lock:
                if not self._check_guard(stop):
                    return False
                for steering_key in keys:
                    self._send(steering_key,True)
            started = time.monotonic()
            deadline = started+seconds
            while time.monotonic()<deadline:
                if stop.wait(min(.01,max(0,deadline-time.monotonic()))) or not self._check_guard(stop):
                    return False
            completed = self._check_guard(stop)
            return completed
        finally:
            self.last_duration = time.monotonic()-started if started is not None else 0.
            if not completed:
                self.release()
            else:
                with self._lock:
                    for steering_key in keys:
                        if steering_key in self.held:
                            self._send(steering_key,False)

    def pulse(self, key, seconds, stop):
        started = None
        try:
            self.release()
            if not self._check_guard(stop):
                return False
            self.send(key, True)
            started = time.monotonic()
            deadline = started + seconds
            while True:
                remaining = deadline-time.monotonic()
                if remaining <= 0:
                    break
                if stop.wait(min(.01, remaining)) or not self._check_guard(stop):
                    return False
            return True
        finally:
            self.last_duration = time.monotonic()-started if started is not None else 0.
            self.release()
