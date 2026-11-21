"""Classic map route prototype. Run with python app.py on Windows."""
import base64
import ctypes
import io
import json
import math
from pathlib import Path
import queue
import random
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import cv2
import numpy as np
from PIL import Image, ImageGrab, ImageTk

from core import Follower, CurveFollower, SteeringTargetError, route_curve, ProgressMonitor, TurnTiming, distance, heading, angle_error, timed_route
from tracking import ArrowTracker
from projection import MapProjection, validate_bounds
from win_input import Keyboard, stop_hotkey_pressed, game_focused, foreground_game_region, focus_game, overlay_select_pressed, input_modifiers_held
from zone_maps import ASSETS, MapCatalog, ZoneDetector, title_unchanged
from diagnostics import ScanRecord, latest_report, record_route_error, MovementRecord
from telemetry import AddonTracker, find_panel
from settings import load_settings, save_settings
from viewport import Viewport
from navigation import ZoneNavigation, NavigationError
from movement import decide, recover_route, JumpSchedule, SteeringPolicy, TimingNoise, SpeedEstimate, MotionHistory
from road_network import RoadStore, plan_road_route
from road_editor import RoadPenEditor
from road_curves import flatten
from atlas import Atlas
from atlas_browser import AtlasBrowser
from continent_routing import ContinentTracker, ContinentNavigation, plan_continent_route
from map_profiles import available_catalogs, classic_catalog, has_classic_install, label as map_profile_label, startup_catalog, remember_catalog
from profile_navigation import load_zone_navigation
from overlay_maps import client_maps, road_data, map_destination
from desktop_overlay import PanelReader, FreshViewport, MapViewport, RoadOverlay


class App:
    def __init__(self, root, catalog=None):
        self.root = root
        root.title('Route Me — Classic map prototype')
        root.geometry('1100x800')
        root.protocol('WM_DELETE_WINDOW', self.close)
        self.region = None
        self.addon_map_id = None
        self.reference = None
        self.display = None
        self.plan_image = None
        self.live_bounds = None
        self.plan_bounds = None
        self.view = 'live'
        self.bounds_anchor = None
        self.map_name = ''
        self.zone_id = None
        self.title_bounds = None
        self.catalog = catalog or MapCatalog()
        if self.catalog.profile:
            root.title(f'Route Me — {map_profile_label(self.catalog)}')
        self.atlas = Atlas(self.catalog)
        self.connection = None
        self.overlay_reader = PanelReader()
        self.overlay_enabled = tk.BooleanVar(value=True)
        self.overlay = None
        self._overlay_route = False
        self._closing = False
        self._overlay_context = None
        self._overlay_pose = FreshViewport()
        self._overlay_pending = None
        self._overlay_maps = None
        self._overlay_signature = None
        self._overlay_roads = {}
        self._overlay_route_key = None
        self._overlay_route_segments = []
        self.world_destination = None
        self.reconnect_world_destination = None
        self.preparing = False
        self.detector = ZoneDetector(self.catalog)
        self.zone_choice = tk.StringVar(value='Automatic')
        self.center = self.tip = None
        self.color_sample = None
        self.points = []
        self.navigation = None
        self.nav_packet = None
        self.planned = None
        self.nav_error = ''
        self.reconnect_route = None
        self.reconnect_destination = None
        if self.catalog.profile:
            legacy=Atlas(classic_catalog())
            self.road_store=RoadStore(self.catalog.root/'roads-world',self.atlas.transform,legacy.transform,self.catalog.profile['profile_id'])
        else:
            self.road_store = RoadStore()
        self.roads = []
        self.road_curves = []
        self.pen = RoadPenEditor(self)
        self.road_draft = []
        self.road_destination = None
        self.road_journey = None
        self.destination_pending = False
        self.destination_press = None
        self.road_load_error = ''
        self.routing_mode = tk.StringVar(value='destination')
        self.road_zone = tk.StringVar(value='Select a map')
        self.mode = self.routing_mode.get()
        self.scale = 1
        self.viewport = Viewport()
        self.pan_anchor = None
        self.offset = (0, 0)
        self.drawing = False
        self.path_cursor = None
        self.thread = None
        self.stop = threading.Event()
        self.events = queue.Queue(maxsize=3)
        self.status = tk.StringVar(value='Choose a map and Draw roads to save your network. Connect, then choose Click destination to travel.')
        self.tolerance = tk.IntVar(value=65)
        self.confidence = tk.DoubleVar(value=.60)
        self.stall_seconds = tk.IntVar(value=20)
        self.timed_mode = tk.BooleanVar(value=False)
        calibration = load_settings()
        self.move_speed = tk.StringVar(value=str(calibration['move_speed']))
        self.stealth = tk.BooleanVar(value=False)
        self.turn_rate = tk.DoubleVar(value=calibration['turn_rate'])
        self.road_half_width = tk.DoubleVar(value=calibration.get('road_half_width',.4))
        self.current = None
        self.buttons = []

        top = ttk.Frame(root, padding=12)
        top.pack(fill='x')
        ttk.Label(top, text='ROUTE ME', font=('Segoe UI', 18, 'bold')).pack(side='left')
        self.map_label = ttk.Label(top, text='Connect to load your current zone')
        self.map_label.pack(side='left', padx=20)
        self.badge = ttk.Label(top, text='STOPPED', foreground='#a63b32')
        self.badge.pack(side='right')
        versions = ttk.Frame(root, padding=(12,0,12,8))
        versions.pack(fill='x')
        ttk.Label(versions,text='Maps').pack(side='left',padx=(0,8))
        self.map_catalogs = {map_profile_label(c): c for c in available_catalogs(current=self.catalog)}
        self.map_version = tk.StringVar(value=map_profile_label(self.catalog))
        self.map_version_picker = ttk.Combobox(versions,textvariable=self.map_version,
            values=list(self.map_catalogs),state='readonly',width=28)
        self.map_version_picker.pack(side='left',padx=(0,12))
        self.map_version_picker.bind('<<ComboboxSelected>>',lambda event:self.change_map_version())
        self.buttons.append(self.map_version_picker)
        self.map_version_hint = ttk.Label(versions,wraplength=750)
        self.map_version_hint.pack(side='left')
        self.update_map_version_hint()
        bar = ttk.Frame(root, padding=(12,0,12,8))
        bar.pack(fill='x')
        for name, command in [('Connect',self.connect_addon),('Clear trip',self.clear),('Save trip',self.save),('Load trip',self.load)]:
            button = ttk.Button(bar, text=name, command=command)
            button.pack(side='left', padx=(0,4))
            self.buttons.append(button)
        self.run_button = ttk.Button(bar, text='Start route', command=lambda:self.start(True))
        self.run_button.pack(side='left', padx=(12,4))
        ttk.Button(bar, text='STOP / Backspace', command=self.halt).pack(side='left')
        advanced_button = ttk.Button(bar, text='Advanced', command=self.toggle_advanced)
        advanced_button.pack(side='right')
        self.buttons.append(advanced_button)

        overlay_bar = ttk.Frame(root,padding=(12,0,12,6))
        overlay_bar.pack(fill='x')
        ttk.Checkbutton(overlay_bar,text='Desktop map overlay',variable=self.overlay_enabled,
                        command=self.hide_overlay).pack(side='left')
        ttk.Label(overlay_bar,text='Open WoW’s map; hold Alt and click a destination. Borderless/windowed mode.').pack(side='left',padx=10)

        roads_bar = ttk.Frame(root,padding=(12,0,12,8))
        roads_bar.pack(fill='x')
        self.road_picker=ttk.Combobox(roads_bar,textvariable=self.road_zone,
            values=sorted(z['name'] for z in self.catalog.zones),state='readonly',width=22)
        self.road_picker.pack(side='left',padx=(0,10))
        self.road_picker.bind('<<ComboboxSelected>>',lambda event:self.open_road_map())
        self.buttons.append(self.road_picker)
        for label,value in [('Click destination','destination'),('Draw roads','roads'),('Pen / edit','pen'),('Draw trip','route')]:
            button=ttk.Radiobutton(roads_bar,text=label,variable=self.routing_mode,value=value,command=self.set_routing_mode)
            button.pack(side='left',padx=(0,10));self.buttons.append(button)
        button=ttk.Button(roads_bar,text='Undo last road',command=self.undo_road)
        button.pack(side='left');self.buttons.append(button)
        self.pen_tools=ttk.Frame(root,padding=(12,0,12,6))
        for label,command in [('New road',self.pen.new_road),('Finish road',self.pen.finish),
                              ('Smooth anchor',lambda:self.pen.change_node(True)),('Corner anchor',lambda:self.pen.change_node(False)),
                              ('Delete anchor',self.pen.delete_anchor),('Delete road',self.pen.delete_road),('Undo edit',self.pen.undo)]:
            button=ttk.Button(self.pen_tools,text=label,command=command)
            button.pack(side='left',padx=(0,4));self.buttons.append(button)

        # Hidden by default: diagnostics, calibration, and the image-based fallback.
        self.advanced_panel = ttk.LabelFrame(root, text='Calibration and diagnostics', padding=8)
        calibration_row = ttk.Frame(self.advanced_panel)
        calibration_row.pack(fill='x')
        for label,variable in [('Normal speed (map px/s)',self.move_speed),('Turn rate (°/s)',self.turn_rate)]:
            ttk.Label(calibration_row,text=label).pack(side='left',padx=(0,4))
            entry = ttk.Entry(calibration_row,textvariable=variable,width=7)
            entry.pack(side='left',padx=(0,12))
            entry.bind('<FocusOut>',lambda event:self.persist_calibration())
            self.buttons.append(entry)
        for name,kind in [('Measure speed',True),('Measure turn rate','turn')]:
            button = ttk.Button(calibration_row,text=name,command=lambda kind=kind:self.start(True,measure=kind))
            button.pack(side='left',padx=4)
            self.buttons.append(button)
        options_row = ttk.Frame(self.advanced_panel)
        options_row.pack(fill='x',pady=6)
        for label,variable in [('Timed route',self.timed_mode),('Stealth override (legacy input)',self.stealth)]:
            toggle = ttk.Checkbutton(options_row,text=label,variable=variable)
            toggle.pack(side='left',padx=(0,10))
            self.buttons.append(toggle)
        self.preview_button = ttk.Button(options_row,text='Preview tracking',command=lambda:self.start(False))
        self.preview_button.pack(side='left',padx=4)
        ttk.Button(options_row,text='Last scan image',command=self.show_last_scan).pack(side='left',padx=4)
        legacy = ttk.Menubutton(options_row,text='Image fallback ▾')
        menu = tk.Menu(legacy,tearoff=False)
        for label,command in [('Scan map',self.find_current_zone),('Capture region',self.select_region),
                              ('Calibrate arrow',self.calibrate),('Load map image',self.load_plan_image),
                              ('Set live map bounds',lambda:self.set_bounds('live')),
                              ('Set reference map bounds',lambda:self.set_bounds('plan')),
                              ('Show live map',lambda:self.show_view('live')),
                              ('Show complete map',lambda:self.show_view('plan'))]:
            menu.add_command(label=label,command=command)
        legacy.configure(menu=menu)
        legacy.pack(side='left',padx=4)
        self.buttons.append(legacy)
        tuning = ttk.Frame(self.advanced_panel)
        road_tuning=ttk.Frame(self.advanced_panel)
        road_tuning.pack(fill='x',pady=(0,6))
        ttk.Label(road_tuning,text='Roads-only half-width (yd)').pack(side='left',padx=(0,4))
        width_entry=ttk.Spinbox(road_tuning,textvariable=self.road_half_width,from_=.1,to=3.,increment=.1,width=5,
                              command=self.persist_calibration)
        width_entry.pack(side='left')
        width_entry.bind('<FocusOut>',lambda event:self.persist_calibration())
        self.buttons.append(width_entry)
        ttk.Label(road_tuning,text='Usable space on each side of your line; no obstacle checks.').pack(side='left',padx=8)
        tuning.pack(fill='x')
        self.zone_picker = ttk.Combobox(tuning,textvariable=self.zone_choice,
            values=['Automatic']+sorted(z['name'] for z in self.catalog.zones),state='readonly',width=20)
        self.zone_picker.pack(side='left',padx=(0,8))
        self.buttons.append(self.zone_picker)
        for label,var,lo,hi,step in [('Color tolerance',self.tolerance,10,150,5),
                                   ('Match',self.confidence,.4,.95,.05),('Stall timeout',self.stall_seconds,10,60,5)]:
            ttk.Label(tuning,text=label).pack(side='left',padx=4)
            spin = ttk.Spinbox(tuning,textvariable=var,from_=lo,to=hi,increment=step,width=5)
            spin.pack(side='left',padx=(0,8))
            self.buttons.append(spin)
        self.map_tools = ttk.Frame(root,padding=(12,0,12,6))
        self.map_tools.pack(fill='x')
        ttk.Button(self.map_tools,text='−',width=3,command=lambda:self.zoom_map(.8)).pack(side='left')
        ttk.Button(self.map_tools,text='+',width=3,command=lambda:self.zoom_map(1.25)).pack(side='left')
        ttk.Button(self.map_tools,text='Fit map',command=self.fit_map).pack(side='left',padx=4)
        self.zoom_label = ttk.Label(self.map_tools,text='100% · Wheel zooms maps · Right-drag pans')
        self.zoom_label.pack(side='left',padx=8)
        self.road_label=ttk.Label(self.map_tools,text='Roads save automatically')
        self.road_label.pack(side='right',padx=12)
        self.nav_label = ttk.Label(self.map_tools,text='')
        self.nav_label.pack(side='right')
        self.atlas_browser=AtlasBrowser(self)
        self.atlas_browser.attach()
        self.canvas = tk.Canvas(root, background='#17212b', highlightthickness=0)
        self.canvas.pack(fill='both', expand=True, padx=12, pady=4)
        self.canvas.bind('<ButtonPress-1>', self.down)
        self.canvas.bind('<B1-Motion>', self.drag)
        self.canvas.bind('<ButtonRelease-1>', self.up)
        self.canvas.bind('<Double-Button-1>',self.map_double_click)
        self.canvas.bind('<Motion>',self.path_motion)
        self.canvas.bind('<Leave>',lambda event:self.path_motion(None))
        self.canvas.bind('<Return>',lambda event:self.pen_key(self.pen.finish))
        self.canvas.bind('<Delete>',lambda event:self.pen_key(self.pen.delete_anchor))
        self.canvas.bind('<Control-z>',lambda event:self.pen_key(self.pen.undo))
        self.canvas.bind('<Configure>', lambda e: self.render())
        self.canvas.bind('<MouseWheel>', lambda e: self.zoom_map(1.25 if e.delta>0 else .8,e.x,e.y))
        self.canvas.bind('<ButtonPress-3>', self.pan_start)
        self.canvas.bind('<B3-Motion>', self.pan_move)
        self.canvas.bind('<ButtonRelease-3>', lambda e: setattr(self,'pan_anchor',None))
        ttk.Label(root, textvariable=self.status, wraplength=1050, padding=12).pack(fill='x')
        ttk.Label(root, text='W = forward · Left/Right arrows = turn · Q/E = strafe · Backspace / Esc stop. Keep the addon panel visible.',
                  padding=(12, 0, 12, 10)).pack(fill='x')
        root.bind('<Escape>', lambda e:self.pen.cancel() if self.mode=='pen' and not self.busy() else self.halt())
        root.bind('<BackSpace>', lambda e: self.halt() if self.busy() else None)
        root.after(70, self.poll)

    def busy(self):
        return getattr(self,'preparing',False) or (self.thread is not None and self.thread.is_alive())

    def hide_overlay(self):
        self.overlay_reader.reset()
        self._overlay_pose=FreshViewport()
        self._overlay_pending=None
        if self.overlay: self.overlay.hide()

    def queue_overlay_destination(self, view, point):
        if not self.busy() and game_focused() and not stop_hotkey_pressed():
            selected_at=time.monotonic()
            self._overlay_pending=(view,point,selected_at+2.,selected_at)
            self.status.set('Release Alt to plan the selected destination.')

    def poll_overlay(self):
        connection=self.connection
        if connection is not self._overlay_context:
            self.hide_overlay()
            self._overlay_context=connection
        enabled=(not self._closing and self.overlay_enabled.get() and connection is not None
                 and connection.get('packet',{}).get('protocol')==4
                 and game_focused() and not stop_hotkey_pressed())
        packet=self.overlay_reader.poll(connection,connection['region'] if connection else None,enabled and not self.busy())
        if not enabled:
            self.hide_overlay();return
        if packet:
            self._overlay_pose.update(packet,self.overlay_reader.observed_at)
        packet=self._overlay_pose.current()
        if not packet:
            self._overlay_pending=None
            if self.overlay: self.overlay.hide()
            return
        try:
            view=MapViewport.from_packet(packet,foreground_game_region())
            if not view:
                self._overlay_pending=None
                if self.overlay: self.overlay.hide()
                return
            self.atlas.packet_zone(packet)
            signature=(self.catalog.root,tuple((p.name,p.stat().st_mtime_ns,p.stat().st_size)
                                              for p in sorted(self.road_store.root.glob('*.json'))))
            if signature!=self._overlay_signature:
                maps=client_maps(self.atlas)
                data=road_data(self.atlas,self.road_store,maps)
                self._overlay_maps=maps;self._overlay_roads=data['maps']
                self._overlay_signature=signature
            record=self._overlay_maps.get(view.map_id)
            if record is None:
                self._overlay_pending=None
                if self.overlay: self.overlay.hide()
                return
            route_key=(id(self.planned),view.map_id,self.catalog.root)
            if route_key!=self._overlay_route_key:
                from navigation import ZoneTransform
                t=ZoneTransform(record['zone'],record['continent'],*record['bounds'],1,1)
                points=[t.pixel(p) for p in self.planned.world] if self.planned and self.navigation and self.navigation.transform.continent==record['continent'] else []
                self._overlay_route_segments=[(*a,*b) for a,b in zip(points,points[1:])]
                self._overlay_route_key=route_key
            idle=not self.busy() and not self.drawing and self.pen.draft is None
            if not idle: self._overlay_pending=None
            if self._overlay_pending:
                selected,point,deadline,selected_at=self._overlay_pending
                if view!=selected or time.monotonic()>deadline:
                    self._overlay_pending=None
                elif self._overlay_pose.received_at>selected_at and not input_modifiers_held():
                    self._overlay_pending=None
                    self.world_destination=map_destination(self.atlas,self._overlay_maps,packet,view.map_id,*point)
                    self.start_continent_route(from_overlay=True)
                    idle=False
            if self.overlay is None:
                self.overlay=RoadOverlay(self.root,self.queue_overlay_destination)
            interactive=idle and overlay_select_pressed()
            label=(self.status.get() if not idle or self._overlay_pending else
                   'Click a destination; release Alt to travel' if interactive else 'Roads • hold Alt to choose a destination')
            self.overlay.show(view,self._overlay_roads.get(view.map_id,{}).get('segments',()),
                              self._overlay_route_segments,interactive,label,self._overlay_pose.received_at)
        except (OSError,ValueError,KeyError,tk.TclError) as error:
            self._overlay_pending=None
            if self.overlay: self.overlay.hide()
            self.status.set(f'Overlay unavailable: {error}')

    def update_map_version_hint(self):
        profile=self.catalog.profile
        if profile:
            if len(profile['zones'])==len(self.catalog.zones):
                text='Roads only on all maps. Terrain and obstacle checks are unavailable.'
            else:
                updated=', '.join(self.catalog.by_id[int(zone)]['name'] for zone in profile['zones'])
                text=f'Roads only: {updated}. Other zones use 1.12 collision.'
            self.map_version_hint.configure(text=text,foreground='#a66b00')
        else:
            self.map_version_hint.configure(text='Classic maps and existing navigation data.',foreground='')

    def change_map_version(self):
        previous=map_profile_label(self.catalog)
        selected=self.map_version.get()
        if selected==previous: return
        if self.busy():
            self.map_version.set(previous)
            self.status.set('Stop the current action before changing map versions.');return
        try:
            catalog=self.map_catalogs[selected]
            atlas=Atlas(catalog)
            zone_id=self.zone_id if self.zone_id in catalog.by_id else 44
            catalog.image(zone_id)  # Verify the destination map before changing state.
            if catalog.profile:
                legacy=Atlas(classic_catalog())
                store=RoadStore(catalog.root/'roads-world',atlas.transform,legacy.transform,catalog.profile['profile_id'])
            else:
                store=RoadStore()
            if self.pen.draft is not None and len(self.pen.draft)>=2 and not self.pen.finish():
                self.map_version.set(previous);return
            if self.drawing and self.mode=='roads' and len(self.road_draft)>=2 and not self.finish_path():
                self.map_version.set(previous);return
            remember_catalog(catalog)
        except (OSError,ValueError,KeyError,TypeError) as error:
            self.map_version.set(previous)
            self.status.set(f'Could not change maps: {error}');return
        self.cancel_destination_click()
        self.pen.reset()
        self.drawing=False;self.path_cursor=None;self.road_draft=[]
        self.catalog,self.atlas,self.road_store=catalog,atlas,store
        self.detector=ZoneDetector(catalog)
        self.connection=self.region=self.addon_map_id=None
        self.navigation=self.nav_packet=self.planned=None
        self.world_destination=self.reconnect_world_destination=None
        self.reconnect_route=self.reconnect_destination=None
        self.road_destination=self.road_journey=None
        self.bounds_anchor=self.color_sample=self.pan_anchor=None
        self.zone_id=None;self.zone_choice.set('Automatic')
        names=sorted(z['name'] for z in catalog.zones)
        self.road_picker.configure(values=names)
        self.zone_picker.configure(values=['Automatic']+names)
        browser=self.atlas_browser
        browser.atlas=atlas;browser.level=browser.view_zone=None
        browser.roads=[];browser.owners=[];browser.road_continent=None
        browser.continent=atlas.zones[zone_id]['continent']
        browser.update_picker()
        self.road_zone.set(catalog.by_id[zone_id]['name'])
        self.root.title(f'Route Me — {map_profile_label(catalog)}'
                        if catalog.profile else 'Route Me — Classic map prototype')
        self.open_road_map()
        self.update_map_version_hint()
        self.status.set(f'{selected}. Connect to route. Updated zones follow roads without obstacle checks.' if catalog.profile
                        else '1.12 Classic maps selected. Connect again before routing.')

    def toggle_advanced(self):
        if self.advanced_panel.winfo_manager():
            self.advanced_panel.pack_forget()
        else:
            self.advanced_panel.pack(fill='x',padx=12,pady=(0,8),before=self.map_tools)

    def focus_for_action(self):
        try:
            focus_game()
            return True
        except ValueError as error:
            self.status.set(str(error))
            return False

    def persist_calibration(self):
        try:
            save_settings(self.move_speed.get(), self.turn_rate.get(),road_half_width=self.road_half_width.get())
        except (ValueError, tk.TclError):
            pass  # Ignore an incomplete entry; keep the previous saved value.
        except OSError as error:
            self.status.set(f'Could not save calibration: {error}')

    def smooth_path(self):
        if not self.busy() and self.road_destination is not None:
            self.plan_destination()
            return
        if self.busy() or len(self.points)<2:
            return
        # Retain the vertices the user chose; route planning handles corners.
        self.planned = None
        if self.classic_preview_available():
            self.start_classic_preview()
            return
        if self.addon_map_id:
            try:
                if self.navigation is None:
                    raise NavigationError(self.nav_error or 'Connect to load outdoor navigation data.')
                self.planned = self.navigation.plan(self.points,self.nav_packet or {})
                self.status.set('Route checked against extracted ground; height inferred. Cyan line is the checked route; dashed line is your drawing.'
                                if self.navigation.collision_checked() else 'Road-following route ready · no obstacle checks in updated zones.')
            except (NavigationError, OSError) as error:
                self.status.set(f'Route needs attention: {error}')
                try:
                    record_route_error(error,self.points,self.nav_packet)
                except OSError:
                    pass
            self.render()
            return
        self.status.set('Route ready. Corners curve automatically. Check the displayed line, then Start route.')
        self.render()

    def classic_preview_available(self):
        return (has_classic_install(self.catalog) and self.region is None and self.plan_image is not None
                and self.plan_image is self.reference
                and self.zone_id in self.atlas.zones and not self.addon_map_id)

    def start_classic_preview(self):
        self.navigation = None
        self.stop.clear()
        self.preparing = True
        self.status.set('Checking the drawn trip against the Classic navigation mesh…')
        for button in self.buttons+[self.preview_button,self.run_button]:
            button.configure(state='disabled')
        self.render()
        self.thread=threading.Thread(target=self.work_classic_preview,
                                    args=(self.zone_id,self.plan_image.shape,list(self.points)),daemon=True)
        self.thread.start()

    def work_classic_preview(self, zone_id, shape, points):
        navigation=None
        try:
            navigation=ZoneNavigation.load(zone_id,shape,self.stop)
            route=navigation.plan_from_map(points)
            if self.stop.is_set():
                self.emit('done','Classic preview cancelled.')
                return
            self.emit('classic_preview',navigation,route)
            self.emit('done','Classic collision preview ready. Cyan is the checked path.')
        except (OSError,ValueError) as error:
            if navigation is not None and not self.stop.is_set():
                self.emit('classic_preview',navigation,None)
            self.emit('done',f'Classic preview: {error}')

    def load_zone_roads(self):
        self.roads=[];self.road_curves=[];self.pen.reset();self.road_draft=[];self.road_load_error=''
        self.road_destination=self.road_journey=None
        self.destination_pending=False
        if self.zone_id is not None:
            try:
                self.roads,self.road_curves=self.road_store.load_document(self.zone_id)
                if self.plan_image is not None:
                    h,w=self.plan_image.shape[:2]
                    self.roads=[flatten(c,w,h) if c is not None else r for r,c in zip(self.roads,self.road_curves)]
                self.roads=self.road_store.validate_display_roads(self.roads)
            except (OSError,ValueError,TypeError) as error:
                self.road_load_error=f'Could not load saved roads: {error}'
            self.road_zone.set(self.map_name)
        self.road_label.configure(text='Road file needs attention' if self.road_load_error else f'{len(self.roads)} roads · autosaved')

    def open_road_map(self):
        if self.busy(): return
        if self.pen.draft is not None and len(self.pen.draft)>=2 and not self.pen.finish(): return
        self.atlas_browser.level=None
        zone=next((z for z in self.catalog.zones if z['name']==self.road_zone.get()),None)
        if zone is None: return
        if self.zone_id!=zone['id'] or not self.addon_map_id or isinstance(self.navigation,ContinentNavigation):
            image=self.catalog.image(zone['id'])
            self.reference=self.display=self.plan_image=image
            self.region=None;self.addon_map_id=None
            self.navigation=self.nav_packet=self.planned=None
            self.center=self.tip=self.current=None
            self.points=[];self.reconnect_route=None
            self.zone_id,self.map_name=zone['id'],zone['name']
            self.live_bounds=self.plan_bounds=(0,0,image.shape[1],image.shape[0])
            self.title_bounds=None;self.nav_error='Connect the addon before choosing a destination.'
            self.nav_label.configure(text='Road map editor')
            self.map_label.configure(text=self.map_name+' · saved roads')
            self.load_zone_roads()
        self.view='plan';self.viewport.reset()
        self.routing_mode.set('roads');self.set_routing_mode()

    def set_routing_mode(self):
        if self.busy(): return
        if self.mode=='pen' and self.routing_mode.get()!='pen':
            if self.pen.draft is not None and len(self.pen.draft)>=2:
                if not self.pen.finish():
                    self.routing_mode.set('pen');return
            self.pen.cancel()
        self.drawing=False;self.path_cursor=None;self.road_draft=[];self.cancel_destination_click()
        self.mode=self.routing_mode.get()
        messages={
            'browse':'Browse maps: wheel up over a zone to open it; wheel down to zoom out. Click a zone on the continent to open it. Choose Click destination to travel.',
            'roads':'Click to add road vertices; double-click the last vertex to finish and save. Zoom and right-drag to pan as you draw.',
            'pen':'Pen: click or click-drag to place anchors. Click an existing road to edit it. Enter finishes; Esc cancels; Alt-drag separates handles.',
            'destination':'Click to travel. Wheel up over a zone to open it; wheel down to zoom out. Right-drag to pan. Connect once; trips refresh your position.',
            'route':'Click near your player marker, then click to add vertices. Double-click to finish; press Start route to travel.'}
        self.status.set(self.road_load_error or messages[self.mode])
        if self.mode=='route' and self.classic_preview_available():
            self.status.set('Draw your guide on the zone map and double-click to check it against the Classic mesh.')
        if self.mode=='pen' and not self.atlas_browser.active: self.pen_tools.pack(fill='x',before=self.map_tools)
        else: self.pen_tools.pack_forget()
        self.render()

    def pen_key(self,command):
        if self.mode=='pen' and not self.busy() and not self.atlas_browser.active:
            command();return 'break'

    def map_double_click(self,event):
        if self.atlas_browser.active: return
        if self.busy(): return
        if self.mode=='pen':
            self.pen.insert(((event.x-self.offset[0])/self.scale,(event.y-self.offset[1])/self.scale))
            self.drawing=False;return 'break'
        if self.mode in ('roads','route') and self.drawing:
            # Tk replaces the second press with Double-Button-1. The first
            # press already added this endpoint; suppress an exact duplicate.
            self.down(event)
            self.finish_path();return 'break'

    def finish_path(self):
        if self.busy() or not self.drawing or self.mode not in ('roads','route'): return False
        vertices=self.road_draft if self.mode=='roads' else self.points
        if len(vertices)<2:
            self.status.set('Add another vertex before finishing the path.');return False
        if self.mode=='roads':
            try: self.save_road()
            except (OSError,ValueError) as error:
                self.status.set(f'Road could not be saved: {error}');return False
        self.drawing=False;self.path_cursor=None
        if self.mode=='route': self.smooth_path()
        else: self.render()
        return True

    def path_motion(self,event):
        if not self.drawing or self.mode not in ('roads','route') or self.atlas_browser.active: return
        p=self.coordinates(event) if event is not None else None
        if p and self.mode=='route' and self.view=='plan':
            projection=self.projection()
            p=projection.to_live(p) if projection else None
        self.path_cursor=p;self.render()

    def persist_roads(self,roads,curves):
        self.roads=self.road_store.save(self.zone_id,roads,curves)
        self.road_curves=curves
        self.planned=self.road_journey=None
        self.road_label.configure(text=f'{len(self.roads)} roads · autosaved')

    def save_road(self):
        if self.road_load_error: raise ValueError(self.road_load_error)
        image=self.plan_image
        if self.zone_id is None or image is None: raise ValueError('Select a zone map first.')
        # Store original map positions, independent of canvas zoom or capture.
        cleaned=self.road_draft
        normalized=[(p[0]/image.shape[1],p[1]/image.shape[0]) for p in cleaned]
        self.pen.ensure_curves()
        self.persist_roads(self.roads+[normalized],self.road_curves+[None])
        self.road_draft=[]
        self.planned=None;self.road_journey=None
        self.road_label.configure(text=f'{len(self.roads)} roads · autosaved')
        self.status.set('Road saved. Click to begin another road, or choose Click destination. Continent routing welds nearby aligned ends automatically.')

    def undo_road(self):
        if self.busy() or not self.roads or self.road_load_error: return
        try:
            self.pen.ensure_curves()
            self.persist_roads(self.roads[:-1],self.road_curves[:-1])
            self.pen.reset()
            self.planned=None;self.road_journey=None
            self.road_label.configure(text=f'{len(self.roads)} roads · autosaved')
            self.status.set('Last road removed and the network saved.')
        except (OSError,ValueError) as error:
            self.status.set(f'Could not save road change: {error}')
        self.render()

    def plan_destination(self):
        self.planned=None;self.road_journey=None
        try:
            if self.road_load_error: raise NavigationError(self.road_load_error)
            if not self.addon_map_id or self.navigation is None or self.nav_packet is None:
                raise NavigationError('Connect the addon before choosing a destination.')
            journey=plan_road_route(self.roads,self.navigation,self.road_destination,self.nav_packet)
            self.road_journey=journey;self.planned=journey.route
            self.points=list(journey.route.requested)
            # Each destination starts from the latest telemetry, including the
            # end of the preceding trip rather than the original Connect marker.
            self.center=journey.route.pixels[0]
            facing=self.nav_packet.get('facing',self.current[1] if self.current else 0.)
            self.tip=(self.center[0]+15*math.sin(math.radians(facing)),
                      self.center[1]-15*math.cos(math.radians(facing)))
            self.status.set(f'Road journey · {journey.road_yards:.0f} yards on saved roads · '+
                            ('ground checked.' if self.navigation.collision_checked() else 'no obstacle checks in updated zones.'))
        except (NavigationError,OSError,ValueError) as error:
            self.points=[]
            self.status.set(f'Road journey needs attention: {error}')
        self.render()

    def zoom_map(self, factor, x=None, y=None):
        self.cancel_destination_click()
        if self.surface() is None or (self.drawing and self.mode not in ('roads','route')):
            return
        # Keep an unfinished path on its map; zoom and pan remain available.
        if not self.drawing and self.atlas_browser.zoom(factor,x,y): return
        self.viewport.zoom_at(factor, self.canvas.winfo_width()/2 if x is None else x,
                              self.canvas.winfo_height()/2 if y is None else y)
        self.render()

    def fit_map(self):
        self.cancel_destination_click()
        self.viewport.reset()
        self.render()

    def pan_start(self, event):
        self.cancel_destination_click()
        if not self.drawing or self.mode in ('roads','route'):
            self.pan_anchor = (event.x,event.y)

    def pan_move(self, event):
        if self.pan_anchor is not None:
            self.viewport.pan = (self.viewport.pan[0]+event.x-self.pan_anchor[0],
                                 self.viewport.pan[1]+event.y-self.pan_anchor[1])
            self.pan_anchor = (event.x,event.y)
            self.render()

    def connect_addon(self):
        if self.busy():
            return
        if self.pen.draft is not None and len(self.pen.draft)>=2 and not self.pen.finish(): return
        if not self.focus_for_action():
            return
        self.stop.clear()
        self.points = []
        self.road_destination=self.road_journey=None
        self.road_draft=[];self.drawing=False
        self.navigation = self.nav_packet = self.planned = None
        self.nav_error = ''
        self.nav_label.configure(text='Loading ground data…')
        self.center = self.tip = self.current = None
        self.addon_map_id = None
        for button in self.buttons+[self.preview_button, self.run_button]:
            button.configure(state='disabled')
        self.thread = threading.Thread(target=self.acquire_addon, daemon=True)
        self.thread.start()

    def acquire_addon(self):
        message = 'Addon connection cancelled.'
        record = None
        outcome = 'cancelled'
        details = {'source':'addon'}
        try:
            record = ScanRecord()
            if self.stop.is_set() or stop_hotkey_pressed():
                return
            region = foreground_game_region()
            rgb = np.asarray(ImageGrab.grab(bbox=region, all_screens=True).convert('RGB'))
            record.capture(rgb, region)
            bounds, packet = find_panel(rgb)
            details.update(panel_bounds=bounds, packet=packet)
            normalize = lambda name: name.casefold().removeprefix('the ')
            zone = next((z for z in self.catalog.zones if normalize(z['name'])==normalize(packet['name'])), None)
            if zone is None:
                raise ValueError(f'No bundled map for {packet["name"]!r} (UI map {packet["map_id"]}). English zone names are required.')
            image = self.catalog.image(zone['id'])
            capture = (region[0]+bounds[0],region[1]+bounds[1],region[0]+bounds[2],region[1]+bounds[3])
            tracker = AddonTracker(packet['map_id'], image.shape, (0,0))
            tracker.locate(rgb[bounds[1]:bounds[3],bounds[0]:bounds[2]])
            deadline = time.monotonic()+1
            while time.monotonic()<deadline:
                if self.stop.wait(.06) or stop_hotkey_pressed() or not game_focused():
                    return
                found = tracker.locate(np.asarray(ImageGrab.grab(bbox=capture, all_screens=True).convert('RGB')))
                if tracker.packet['sequence'] != packet['sequence']:
                    self.emit('status','Loading extracted ground and obstacle data…')
                    navigation,nav_error=None,''
                    try:
                        navigation=load_zone_navigation(self.catalog,zone['id'],image.shape,self.stop,atlas=self.atlas)
                    except (ValueError,OSError) as error:
                        nav_error=str(error)
                    if self.stop.is_set() or not game_focused():
                        return
                    # Geometry loading can take seconds; refresh the live anchor.
                    found=tracker.locate(np.asarray(ImageGrab.grab(bbox=capture,all_screens=True).convert('RGB')))
                    if navigation:
                        try:
                            ground,alignment=navigation.validate_packet(tracker.packet)
                            details.update(navigation_digest=navigation.digest,alignment_yards=alignment,
                                           tiles=len(navigation.tiles),ground_world=ground,
                                           height_source='inferred from mesh' if navigation.collision_checked(ground) else 'unused; roads only')
                        except NavigationError as error:
                            nav_error=str(error)
                    self.emit('addon', image, capture, zone, tracker.packet, found, navigation, nav_error)
                    message = ((f'{zone["name"]}: green shading is extracted walkable ground. Draw from your marker.'
                                if navigation and navigation.collision_checked() else f'{zone["name"]}: roads-only routing; no obstacle checks. Click a destination or draw a trip.')
                               if not nav_error else f'{zone["name"]} connected. Navigation unavailable: {nav_error}')
                    outcome = 'success'
                    return
            raise ValueError('Addon data is not updating.')
        except Exception as error:
            outcome = 'failed'
            message = f'Addon connection stopped: {error}'
        finally:
            if record:
                try:
                    record.finish(outcome, message, details)
                    if outcome == 'failed':
                        message += ' Open Last scan image to inspect the capture.'
                except Exception as error:
                    message += f' Diagnostic save failed: {error}'
            self.emit('done', message)

    def apply_addon(self, image, region, zone, packet, found, navigation=None, nav_error=''):
        self.atlas_browser.level=None
        self.connection=dict(region=region,zone_id=zone['id'],map_id=packet['map_id'],
                             packet=packet,navigation=navigation,image=image)
        self.world_destination=self.reconnect_world_destination
        self.reconnect_world_destination=None
        self.navigation,self.nav_packet,self.planned=navigation,packet,None
        self.nav_error=nav_error
        self.nav_label.configure(text=((f'{len(navigation.tiles)} ground tiles · '+('alignment pending' if nav_error else 'XY aligned · ground inferred'))
                                  if navigation.collision_checked() else 'Roads only · no obstacle checks') if navigation else 'No collision coverage')
        self.reference = self.display = self.plan_image = image
        self.region = region
        self.addon_map_id = packet['map_id']
        self.zone_id, self.map_name = zone['id'], zone['name']
        self.load_zone_roads()
        if self.reconnect_destination and self.reconnect_destination[0]==zone['id']:
            self.road_destination=self.reconnect_destination[1]
        self.reconnect_destination=None
        self.title_bounds = None
        self.live_bounds = self.plan_bounds = (0,0,image.shape[1],image.shape[0])
        self.center, facing, _ = found
        self.tip = (self.center[0]+15*math.sin(math.radians(facing)), self.center[1]-15*math.cos(math.radians(facing)))
        self.color_sample = None
        self.current = found
        self.points = (self.reconnect_route[1] if self.reconnect_route and self.reconnect_route[0]==zone['id'] else [])
        self.reconnect_route = None
        self.viewport.reset()
        self.stealth.set(packet['stealth'])
        self.view = 'plan'
        self.mode = self.routing_mode.get()
        self.map_label.configure(text=self.map_name+' · addon')
        self.render()

    def find_current_zone(self):
        if self.busy():
            return
        if not (ASSETS / 'classic_arrow.png').is_file():
            self.status.set('Use Connect with the Wayfinder addon for this installation.')
            return
        if not self.focus_for_action():
            return
        forced = next((zone['id'] for zone in self.catalog.zones if zone['name']==self.zone_choice.get()), None)
        self.points = []
        self.center = self.tip = self.current = None
        self.stop.clear()
        for button in self.buttons+[self.preview_button, self.run_button]:
            button.configure(state='disabled')
        self.badge.configure(text='FINDING MAP', foreground='#236a93')
        self.thread = threading.Thread(target=self.acquire, args=(forced,), daemon=True)
        self.thread.start()

    def acquire(self, forced):
        message = 'Scan cancelled.'
        record = None
        outcome = 'cancelled'
        try:
            record = ScanRecord(forced)
            self.detector.diagnostics = {}
            if self.stop.is_set() or stop_hotkey_pressed():
                return
            region = foreground_game_region()
            rgb = np.asarray(ImageGrab.grab(bbox=region, all_screens=True).convert('RGB')).copy()
            record.capture(rgb, region)
            self.emit('status', 'Identifying the zone, aligning its complete map, and finding your arrow…')
            result = self.detector.detect(rgb, forced_zone=forced)
            if self.stop.is_set() or stop_hotkey_pressed():
                return
            self.emit('acquired', rgb, region, result)
            outcome = 'success'
            message = f'{result.zone["name"]} loaded. '+(result.warning if result.warning else 'Switch back here and draw from your player marker. Preview tracking before moving.')
        except Exception as error:
            outcome = 'failed'
            message = f'Map scan stopped: {error}'
        finally:
            if record:
                try:
                    record.finish(outcome, message, self.detector.diagnostics)
                    message += ' Last scan image has the capture and details.' if record.data['image'] else ' No screenshot was captured.'
                except Exception as error:
                    message += f' Could not save the diagnostic report: {error}'
            self.emit('done', message)

    def show_last_scan(self):
        try:
            latest = latest_report()
            if latest is None:
                self.status.set('No saved scan yet. Run Find current zone once with this version.')
                return
            path, report = latest
            window = tk.Toplevel(self.root)
            window.title('Last scan — exact capture and detector report')
            window.geometry('1150x850')
            ttk.Label(window, text=report.get('message', report['status']), wraplength=1100, padding=10).pack(fill='x')
            if report.get('image'):
                screenshot = Image.open(report['image']).convert('RGB')
                screenshot.thumbnail((1110, 580))
                photo = ImageTk.PhotoImage(screenshot)
                label = ttk.Label(window, image=photo)
                label.photo = photo
                label.pack(fill='both', expand=True)
            else:
                ttk.Label(window, text='This attempt stopped before a screenshot was captured.', padding=20).pack()
            details = tk.Text(window, height=11, wrap='word')
            details.pack(fill='both', padx=10, pady=10)
            details.insert('1.0', f'Report: {path}\n\n'+json.dumps(report, indent=2))
            details.configure(state='disabled')
        except Exception as error:
            messagebox.showerror('Cannot open scan report', str(error))

    def apply_acquisition(self, rgb, region, result):
        self.connection=self.world_destination=None;self.atlas_browser.level=None
        self.addon_map_id = None
        self.viewport.reset()
        self.reference = self.display = rgb
        self.region = tuple(region)
        self.plan_image = result.image
        self.live_bounds = result.bounds
        self.plan_bounds = (0, 0, result.image.shape[1], result.image.shape[0])
        self.map_name = result.zone['name']
        self.zone_id = result.zone['id']
        self.load_zone_roads()
        self.title_bounds = result.title_bounds
        self.map_label.configure(text=self.map_name)
        self.points = []
        self.tolerance.set(65)
        self.confidence.set(.60)
        self.center = self.color_sample = self.tip = self.current = None
        if result.arrow:
            self.center, self.color_sample, self.tip, self.current = result.arrow
        self.view = 'plan'
        self.mode = 'route'
        self.routing_mode.set('route')
        self.render()

    def select_region(self):
        if self.busy():
            return
        self.root.withdraw()
        overlay = tk.Toplevel(self.root)
        overlay.overrideredirect(True)
        overlay.attributes('-topmost', True)
        overlay.attributes('-alpha', .35)
        width, height = overlay.winfo_screenwidth(), overlay.winfo_screenheight()
        overlay.geometry(f'{width}x{height}+0+0')
        canvas = tk.Canvas(overlay, bg='black', cursor='crosshair', highlightthickness=0)
        canvas.pack(fill='both', expand=True)
        canvas.create_text(width//2, 40, fill='white', font=('Segoe UI', 20),
                           text='Drag around the map artwork. Keep the player arrow inside. Backspace / Esc cancel.')
        anchor = []
        rectangle = []
        def begin(event):
            anchor[:] = [event.x_root, event.y_root]
            if rectangle:
                canvas.delete(rectangle[0])
            rectangle[:] = [canvas.create_rectangle(event.x, event.y, event.x, event.y, outline='#40ffff', width=3)]
        def move(event):
            if anchor:
                canvas.coords(rectangle[0], *anchor, event.x_root, event.y_root)
        def finish(event):
            if not anchor:
                return
            box = (min(anchor[0], event.x_root), min(anchor[1], event.y_root),
                   max(anchor[0], event.x_root), max(anchor[1], event.y_root))
            overlay.destroy()
            if box[2]-box[0] < 100 or box[3]-box[1] < 100:
                self.root.deiconify()
                return
            def capture():
                try:
                    self.viewport.reset()
                    self.addon_map_id = None
                    self.connection=self.world_destination=None;self.atlas_browser.level=None
                    self.reference = np.asarray(ImageGrab.grab(bbox=box).convert('RGB')).copy()
                    self.display = self.reference
                    self.region = box
                    self.live_bounds = None
                    self.title_bounds = None
                    self.zone_id = None
                    self.load_zone_roads()
                    self.view = 'live'
                    self.center = self.tip = None
                    self.color_sample = None
                    self.points = []
                    self.current = None
                    self.status.set('Captured. Click your position dot, then the light arrow body, then the pointed tip.')
                    self.mode = 'center'
                except Exception as error:
                    messagebox.showerror('Capture failed', str(error))
                finally:
                    self.root.deiconify()
                    self.render()
            self.root.after(350, capture)
        def cancel(event=None):
            overlay.destroy()
            self.root.deiconify()
        canvas.bind('<ButtonPress-1>', begin)
        canvas.bind('<B1-Motion>', move)
        canvas.bind('<ButtonRelease-1>', finish)
        overlay.bind('<Escape>', cancel)
        overlay.focus_force()

    def calibrate(self):
        if self.addon_map_id:
            self.status.set('Addon coordinates and facing need no arrow calibration. Use Connect addon to refresh your position.')
            return
        if self.reference is not None:
            self.view = 'live'
            self.mode = 'center'
            self.display = self.reference
            self.current = None
            self.status.set('Click the marker rotation center (not an offset colored decoration). Automatic setup normally handles this.')
            self.render()

    def draw_mode(self):
        if self.busy(): return
        self.routing_mode.set('route')
        self.set_routing_mode()
        if self.plan_image is not None:
            if self.projection() is None:
                self.status.set('Set live map bounds and reference map bounds before drawing on the complete map.')
                return
            self.view = 'plan'
        self.display = self.reference
        self.status.set('Click near the player arrow, then click to add vertices. Double-click to finish. A new path replaces the trip.')
        self.render()

    def projection(self):
        if self.live_bounds is None or self.plan_bounds is None:
            return None
        return MapProjection(self.live_bounds, self.plan_bounds)

    def surface(self):
        if self.atlas_browser.active: return self.atlas_browser.surface()
        return self.plan_image if self.view == 'plan' else self.display

    def show_view(self, view):
        if view == 'plan' and self.plan_image is None:
            self.status.set('Click Find current zone to load the matching complete map automatically.')
            return
        self.view = view
        self.mode = 'route'
        self.bounds_anchor = None
        self.render()

    def load_plan_image(self):
        path = filedialog.askopenfilename(title='Complete map of the SAME Classic zone',
                                         filetypes=[('Map images', '*.png *.jpg *.jpeg *.webp')])
        if not path:
            return
        try:
            with Image.open(path) as image:
                self.plan_image = np.asarray(image.convert('RGB')).copy()
            from pathlib import Path
            self.map_name = Path(path).name
            self.map_label.configure(text=self.map_name[:36])
            self.plan_bounds = None
            self.zone_id = None
            self.load_zone_roads()
            self.title_bounds = None
            self.points = []
            self.set_bounds('plan')
        except Exception as error:
            messagebox.showerror('Map image failed', str(error))

    def set_bounds(self, view):
        if (self.reference if view == 'live' else self.plan_image) is None:
            self.status.set('Capture the live map or load the complete map image first.')
            return
        self.view = view
        if view == 'live':
            self.display = self.reference
        self.mode = 'bounds'
        self.bounds_anchor = None
        self.status.set('Click the TOP-LEFT of the full zone map artwork (0,0), then BOTTOM-RIGHT (100,100). Exclude borders and sidebars.')
        self.render()

    def clear(self):
        self.world_destination=None
        self.pen.reset()
        self.points = []
        self.planned = None
        self.reconnect_route = None
        self.reconnect_destination = None
        self.road_destination=self.road_journey=None
        self.road_draft=[];self.destination_pending=False;self.drawing=False;self.path_cursor=None
        self.render()

    def coordinates(self, event):
        surface = self.surface()
        if surface is None:
            return None
        x = (event.x-self.offset[0])/self.scale
        y = (event.y-self.offset[1])/self.scale
        if 0 <= x <= surface.shape[1] and 0 <= y <= surface.shape[0]:
            return (x, y)

    def down(self, event):
        if self.busy():
            return
        self.cancel_destination_click()
        if self.mode=='destination' and self.atlas_browser.level!='world' and not (
                self.atlas_browser.active and getattr(event,'state',0)&1):
            if self.coordinates(event) is not None:
                self.destination_press=(event.x,event.y)
                self.destination_pending=True
            return
        if self.atlas_browser.active:
            self.atlas_browser.down(event);return
        if self.mode=='browse':
            return
        if self.mode=='pen':
            if self.zone_id is None or self.plan_image is None or self.view!='plan':
                self.status.set('Select a bundled map before editing roads.');return
            if self.road_load_error:
                self.status.set(self.road_load_error);return
            self.pen.down(((event.x-self.offset[0])/self.scale,(event.y-self.offset[1])/self.scale))
            return
        p = self.coordinates(event)
        if p is None:
            return
        if self.mode == 'roads':
            if self.zone_id is None or self.plan_image is None or self.view!='plan':
                self.status.set('Select a bundled map to draw reusable roads.')
                return
            if self.road_load_error:
                self.status.set(self.road_load_error)
                return
            if not self.drawing: self.road_draft=[];self.drawing=True
            if not self.road_draft or distance(p,self.road_draft[-1])>1e-6: self.road_draft.append(p)
            self.path_cursor=None
            self.render()
            return
        if self.mode == 'bounds':
            if self.bounds_anchor is None:
                self.bounds_anchor = p
                self.status.set('Now click the BOTTOM-RIGHT of the same full zone map artwork (100,100).')
            else:
                try:
                    bounds = validate_bounds((*self.bounds_anchor, *p))
                except ValueError as error:
                    self.status.set(str(error))
                    self.bounds_anchor = None
                    return
                if self.view == 'live':
                    self.live_bounds = bounds
                else:
                    self.plan_bounds = bounds
                self.points = []
                self.bounds_anchor = None
                self.mode = 'route'
                projection = self.projection()
                if projection and self.center:
                    x, y = projection.zone_coordinates(self.center)
                    self.status.set(f'Maps registered. Arrow estimate: {x:.1f}, {y:.1f}. Compare with the in-game Player label; then Draw route.')
                else:
                    self.status.set('Map bounds set. Set the other map bounds and calibrate the arrow next.')
            self.render()
            return
        if self.view == 'plan':
            projection = self.projection()
            if projection is None:
                self.status.set('Set both map bounds before drawing.')
                return
            p = projection.to_live(p)
        if self.mode == 'center':
            self.center = p
            self.points = []
            self.tip = None
            self.color_sample = None
            self.mode = 'body'
            self.status.set('Now click inside the light, solid arrow body, away from colored details and the dark outline.')
        elif self.mode == 'body':
            self.color_sample = p
            self.mode = 'tip'
            self.status.set('Now click the pointed arrow tip to establish facing.')
        elif self.mode == 'tip':
            self.tip = p
            try:
                tracker = ArrowTracker(self.reference, self.center, self.tip, self.tolerance.get(), self.confidence.get(), self.color_sample)
                found = tracker.locate(self.reference)
                if found is None:
                    raise ValueError('Calibration is ambiguous. Try a clearer crop or a different color tolerance.')
            except Exception as error:
                self.tip = None
                self.status.set(str(error))
                self.mode = 'center'
            else:
                self.mode = 'route'
                self.status.set('Arrow calibrated. Click near the arrow, then add vertices along walkable roads. Double-click to finish.')
        else:
            if not self.drawing:
                offline_classic=self.classic_preview_available()
                if not offline_classic and (self.center is None or self.tip is None):
                    self.status.set('Calibrate the arrow before drawing a route.');return
                if not offline_classic and distance(p, self.center) > 15:
                    self.status.set('Start the drawing within 15 map pixels of the calibrated player arrow.');return
                self.points=[p if offline_classic else self.center];self.drawing=True
                self.world_destination=None
                self.road_destination=self.road_journey=None;self.planned=None
            if distance(p,self.points[-1])>1e-6: self.points.append(p)
            self.path_cursor=None
        self.render()

    def cancel_destination_click(self):
        self.destination_pending=False;self.destination_press=None

    def choose_destination(self, event):
        if self.atlas_browser.active:
            return self.atlas_browser.choose_destination(event)
        p=self.coordinates(event)
        if p is None: return False
        if self.view=='plan':
            projection=self.projection()
            if projection is None:
                self.status.set('Set both map bounds before choosing a destination.');return False
            p=projection.to_live(p)
        if self.connection and (not self.addon_map_id or isinstance(self.navigation,ContinentNavigation)):
            if self.zone_id not in self.atlas.zones:
                self.status.set('Select a supported outdoor destination map.');return False
            t=self.atlas.transform(self.zone_id)
            self.world_destination=dict(continent=t.continent,world=t.world(p,0.),zone=self.zone_id)
            self.road_destination=None
        else:
            if not self.addon_map_id or self.navigation is None:
                self.status.set('Connect the addon before choosing a destination.');return False
            self.world_destination=None;self.road_destination=p
        self.planned=self.road_journey=None;self.points=[]
        self.render();return True

    def drag(self, event):
        if self.destination_pending and distance((event.x,event.y),self.destination_press)>5:
            self.cancel_destination_click()
        if self.mode=='pen' and not self.busy():
            self.pen.drag(((event.x-self.offset[0])/self.scale,(event.y-self.offset[1])/self.scale),
                          independent=bool(getattr(event,'state',0)&0x20008))
            return
        if self.drawing and self.mode in ('roads','route'):
            self.path_motion(event);return

    def up(self, event):
        if self.mode=='pen':
            self.pen.up();return
        if self.mode in ('roads','route'): return
        if self.destination_pending:
            press=self.destination_press
            self.cancel_destination_click()
            if not self.busy() and distance((event.x,event.y),press)<=5 and self.choose_destination(event):
                self.start(True)
            return

    def render(self):
        if self.mode=='pen' and not self.atlas_browser.active and not self.pen_tools.winfo_manager(): self.pen_tools.pack(fill='x',before=self.map_tools)
        elif (self.mode!='pen' or self.atlas_browser.active) and self.pen_tools.winfo_manager(): self.pen_tools.pack_forget()
        self.canvas.delete('all')
        if self.atlas_browser.active:
            self.atlas_browser.render();return
        surface = self.surface()
        if surface is None:
            self.canvas.create_text(400, 160, text='Choose a map to draw roads, or Connect to travel', fill='#cad9e6', font=('Segoe UI', 20))
            return
        h, w = surface.shape[:2]
        cw, ch = max(1, self.canvas.winfo_width()), max(1, self.canvas.winfo_height())
        self.viewport.layout(cw,ch,w,h)
        self.scale, self.offset = self.viewport.scale, self.viewport.offset
        # Render only the visible canvas, even at 8x zoom.
        transform = (1/self.scale,0,-self.offset[0]/self.scale,0,1/self.scale,-self.offset[1]/self.scale)
        visible = Image.fromarray(surface).transform((cw,ch),Image.Transform.AFFINE,transform,
                                                     resample=Image.Resampling.BILINEAR,fillcolor='#17212b')
        if self.navigation and (self.addon_map_id or self.classic_preview_available()):
            ground_transform=tuple(v*self.navigation.overlay_scale for v in transform)
            ground=Image.fromarray(self.navigation.overlay).transform((cw,ch),Image.Transform.AFFINE,ground_transform,
                resample=Image.Resampling.NEAREST)
            visible=Image.alpha_composite(visible.convert('RGBA'),ground)
        self.photo = ImageTk.PhotoImage(visible)
        self.canvas.create_image(0,0, anchor='nw', image=self.photo)
        self.zoom_label.configure(text=f'{self.viewport.zoom:.0%} · Wheel zooms maps · Right-drag pans')
        def surface_xy(p):
            return (p[0]*self.scale+self.offset[0], p[1]*self.scale+self.offset[1])
        projection = self.projection()
        def xy(p):
            if self.view == 'plan' and projection:
                p = projection.to_plan(p)
            return surface_xy(p)
        bounds = self.plan_bounds if self.view == 'plan' else self.live_bounds
        if bounds:
            self.canvas.create_rectangle(*surface_xy(bounds[:2]), *surface_xy(bounds[2:]), outline='#6ba8ff', width=2, dash=(5, 4))
        if self.bounds_anchor:
            x, y = surface_xy(self.bounds_anchor)
            self.canvas.create_oval(x-5, y-5, x+5, y+5, outline='#6ba8ff', width=2)
        if self.view == 'plan' and projection is None:
            return
        if self.view=='plan' and self.zone_id is not None and self.plan_image is not None:
            height,width=self.plan_image.shape[:2]
            for index,road in enumerate(self.roads):
                if self.mode=='pen' and index==self.pen.road and self.pen.draft is None: continue
                coords=[v for p in road for v in surface_xy((p[0]*width,p[1]*height))]
                self.canvas.create_line(*coords,fill='#779bd0',width=4)
            if len(self.road_draft)>1:
                self.canvas.create_line(*[v for p in self.road_draft for v in surface_xy(p)],fill='#b3d1ff',width=4)
        if self.road_destination is not None:
            x,y=xy(self.road_destination)
            self.canvas.create_oval(x-7,y-7,x+7,y+7,outline='#ffffff',width=2)
            self.canvas.create_text(x+12,y+12,anchor='w',text='Destination',fill='white')
        if self.road_journey is not None:
            for label,p in [('Join road',self.road_journey.entrance),('Leave road',self.road_journey.exit)]:
                x,y=xy(p)
                self.canvas.create_oval(x-4,y-4,x+4,y+4,fill='#a8c8ff',outline='')
                self.canvas.create_text(x+8,y-12,anchor='w',text=label,fill='#d4e5ff')
        if len(self.points) > 1:
            checks_ground = self.addon_map_id or self.classic_preview_available()
            if checks_ground:
                drawn = self.planned.pixels if self.planned else self.points
                if self.planned:
                    guide=[v for p in self.points for v in xy(p)]
                    self.canvas.create_line(*guide,fill='#e7ca89',width=1,dash=(4,4))
            else:
                drawn=self.points
            coords = [v for p in drawn for v in xy(p)]
            if len(coords)>=4:
                self.canvas.create_line(*coords, fill='#29edca' if self.planned or not checks_ground else '#ed9e64', width=3, arrow='last')
        if self.drawing and self.mode in ('roads','route'):
            vertices=self.road_draft if self.mode=='roads' else self.points
            transform=surface_xy if self.mode=='roads' else xy
            for vertex in vertices:
                x,y=transform(vertex)
                self.canvas.create_oval(x-3,y-3,x+3,y+3,fill='#e1efff',outline='#17212b')
            if vertices and self.path_cursor is not None:
                self.canvas.create_line(*transform(vertices[-1]),*transform(self.path_cursor),fill='#d4e5ff',width=2,dash=(4,4))
        if self.center:
            x, y = xy(self.center)
            self.canvas.create_oval(x-5, y-5, x+5, y+5, outline='#ffc76b', width=2)
        if self.center and self.tip:
            self.canvas.create_line(*xy(self.center), *xy(self.tip), fill='#ffc76b', width=2, arrow='last')
        if self.current:
            import math
            position, direction, score = self.current
            if self.view == 'plan' and projection:
                direction = projection.plan_facing(direction)
            x, y = xy(position)
            self.canvas.create_oval(x-12, y-12, x+12, y+12, outline='#ff62c7', width=2)
            self.canvas.create_line(x, y, x+25*math.sin(math.radians(direction)),
                                    y-25*math.cos(math.radians(direction)), fill='#ff62c7', width=3, arrow='last')
            if projection:
                zx, zy = projection.zone_coordinates(position)
                self.canvas.create_text(x+16, y-20, anchor='w', fill='#ffb0e1',
                                        text=f'{zx:.1f}, {zy:.1f}', font=('Segoe UI', 11, 'bold'))
        self.pen.render(surface_xy)

    def save(self):
        if self.reference is None:
            return
        if self.classic_preview_available():
            self.status.set('Classic collision previews are temporary. Use Draw roads to save your line.')
            return
        path = filedialog.asksaveasfilename(defaultextension='.route.json', filetypes=[('Route', '*.route.json')])
        if not path:
            return
        try:
            buffer = io.BytesIO()
            Image.fromarray(self.reference).save(buffer, format='PNG')
            data = dict(version=2, region=self.region, center=self.center, tip=self.tip, points=self.points,
                        tolerance=self.tolerance.get(), confidence=self.confidence.get(),
                        image=base64.b64encode(buffer.getvalue()).decode('ascii'),
                        color_sample=self.color_sample, live_bounds=self.live_bounds,
                        plan_bounds=self.plan_bounds, map_name=self.map_name,
                        zone_id=self.zone_id, title_bounds=self.title_bounds,
                        addon_map_id=self.addon_map_id)
            if self.road_destination is not None:
                data['road_destination']=self.road_destination
            if self.world_destination is not None:
                data['world_destination']=self.world_destination
            if self.plan_image is not None:
                plan_buffer = io.BytesIO()
                Image.fromarray(self.plan_image).save(plan_buffer, format='PNG')
                data['plan_image'] = base64.b64encode(plan_buffer.getvalue()).decode('ascii')
            with open(path, 'w', encoding='utf-8') as stream:
                json.dump(data, stream)
            self.status.set('Route saved locally, including the cropped reference map. Reuse requires identical map layout and starting position.')
        except Exception as error:
            messagebox.showerror('Save failed', str(error))

    def load(self):
        path = filedialog.askopenfilename(filetypes=[('Route', '*.route.json')])
        if not path:
            return
        try:
            with open(path, encoding='utf-8') as stream:
                data = json.load(stream)
            if data.get('version') not in (1, 2):
                raise ValueError('Unsupported route format.')
            world_destination=data.get('world_destination')
            if world_destination is not None:
                if not isinstance(world_destination,dict): raise ValueError('Invalid continent destination.')
                continent=world_destination.get('continent');zone_id=world_destination.get('zone')
                world=world_destination.get('world')
                if (type(continent) is not int or continent not in self.atlas.continents or type(zone_id) is not int or
                    zone_id not in self.atlas.zones or self.atlas.zones[zone_id]['continent']!=continent or
                    not isinstance(world,(list,tuple)) or len(world)!=3 or
                    not all(isinstance(v,(int,float)) and not isinstance(v,bool) and math.isfinite(v) for v in world)):
                    raise ValueError('Invalid continent destination.')
                t=self.atlas.transform(zone_id);pixel=t.pixel(world)
                if not (0<=pixel[0]<=t.width and 0<=pixel[1]<=t.height):
                    raise ValueError('Continent destination lies outside its zone map.')
            reference = np.asarray(Image.open(io.BytesIO(base64.b64decode(data['image']))).convert('RGB')).copy()
            region = tuple(map(int, data['region']))
            if len(region) != 4 or (not data.get('addon_map_id') and reference.shape[:2] != (region[3]-region[1], region[2]-region[0])):
                raise ValueError('Route image and capture region do not match.')
            points = [tuple(map(float, p)) for p in data['points']]
            center, tip = data['center'], data['tip']
            if center is not None and tip is not None and not data.get('addon_map_id'):
                ArrowTracker(reference, center, tip, data['tolerance'], data['confidence'], data.get('color_sample'))
            plan_image = None
            if data.get('plan_image'):
                plan_image = np.asarray(Image.open(io.BytesIO(base64.b64decode(data['plan_image']))).convert('RGB')).copy()
            live_bounds, plan_bounds = data.get('live_bounds'), data.get('plan_bounds')
            if live_bounds:
                validate_bounds(live_bounds)
            if plan_bounds:
                validate_bounds(plan_bounds)
            self.region, self.reference, self.display = region, reference, reference
            self.connection=None;self.atlas_browser.level=None
            self.world_destination=world_destination
            self.reconnect_world_destination=world_destination if data.get('addon_map_id') else None
            self.addon_map_id = data.get('addon_map_id')
            self.navigation = self.nav_packet = self.planned = None
            self.nav_error = 'Loaded routes require Connect to refresh position and navigation geometry.'
            self.nav_label.configure(text='Reconnect to validate ground')
            self.reconnect_route = (data.get('zone_id'),points) if self.addon_map_id and world_destination is None else None
            self.center, self.tip, self.points = center, tip, points
            self.color_sample = data.get('color_sample')
            self.live_bounds, self.plan_bounds = live_bounds, plan_bounds
            self.plan_image = plan_image
            self.map_name = data.get('map_name', '')
            self.zone_id = data.get('zone_id')
            self.load_zone_roads()
            if data.get('road_destination') is not None:
                destination=data['road_destination']
                if len(destination)!=2 or not all(isinstance(v,(int,float)) and math.isfinite(v) for v in destination):
                    raise ValueError('Invalid saved destination.')
                self.road_destination=tuple(destination)
                self.reconnect_destination=(self.zone_id,self.road_destination) if self.addon_map_id else None
            self.title_bounds = data.get('title_bounds')
            self.map_label.configure(text=self.map_name[:36] or 'No reference map loaded')
            self.view = 'plan' if plan_image is not None else 'live'
            self.tolerance.set(data['tolerance'])
            self.confidence.set(data['confidence'])
            self.current = None
            self.mode = 'destination' if self.road_destination is not None or world_destination else 'route'
            self.routing_mode.set(self.mode)
            self.status.set('Loaded. Connect to refresh position and check the saved route against ground data.' if self.addon_map_id else
                            'Loaded. Restore the exact map layout and starting position; verify with Preview tracking before moving.')
            self.viewport.reset()
            if world_destination:
                self.atlas_browser.show_continent(world_destination['continent'])
                self.routing_mode.set('destination');self.set_routing_mode()
                self.status.set('Loaded continent destination. Connect to refresh your live zone before starting.')
            self.render()
        except Exception as error:
            messagebox.showerror('Load failed', str(error))

    def start(self, movement, measure=False):
        if self.busy():
            return
        if self.classic_preview_available():
            self.status.set('Classic preview is offline. Choose Draw trip to check a route on the map.')
            return
        if self.drawing and self.mode in ('roads','route'):
            self.status.set('Double-click to finish the path before starting.');return
        if movement and not measure and self.world_destination is not None:
            self.start_continent_route();return
        focused = False
        try:
            if self.reference is None or self.center is None or self.tip is None:
                raise ValueError('Connect the addon first, or use the legacy image scanner.')
            if movement and not measure and self.addon_map_id:
                if self.navigation is None:
                    raise NavigationError(self.nav_error or 'Connect to load outdoor navigation data.')
                if self.road_destination is not None:
                    if self.road_load_error:
                        raise NavigationError(self.road_load_error)
                    if not self.roads:
                        raise NavigationError('No saved roads in this zone. Draw roads first.')
                elif len(self.points)<2:
                    raise NavigationError('Draw a trip or click a destination first.')
            tolerance, confidence, stall_timeout = self.tolerance.get(), self.confidence.get(), self.stall_seconds.get()
            if not (10 <= tolerance <= 150 and .4 <= confidence <= .95 and 10 <= stall_timeout <= 60):
                raise ValueError('Settings must stay within the displayed spinbox limits.')
            tracker = (ContinentTracker(self.atlas,self.navigation.transform.continent,self.center)
                       if isinstance(self.navigation,ContinentNavigation) else
                       AddonTracker(self.addon_map_id,self.reference.shape,self.center) if self.addon_map_id else
                       ArrowTracker(self.reference,self.center,self.tip,tolerance,confidence,self.color_sample))
            if movement and self.plan_image is not None and self.projection() is None:
                raise ValueError('Set both map bounds before using the complete reference map.')
            timed = movement and self.timed_mode.get()
            if timed and self.addon_map_id and not measure:
                raise NavigationError('Collision routing requires live feedback. Turn off Timed route.')
            speed, turn_rate = None, float(self.turn_rate.get())
            if not math.isfinite(turn_rate) or turn_rate<=0:
                raise ValueError('Turn rate must be positive and finite.')
            if timed and not measure:
                try:
                    speed, turn_rate = float(self.move_speed.get()), float(self.turn_rate.get())
                except (ValueError, tk.TclError):
                    raise ValueError('Measure speed first, or enter speed in live-map pixels/second and turn rate in degrees/second.')
                if not all(math.isfinite(value) and value > 0 for value in (speed, turn_rate)):
                    raise ValueError('Speed and turn rate must be positive, finite numbers.')
            if movement and not measure:
                if isinstance(tracker, AddonTracker):
                    if not self.focus_for_action():
                        return
                    focused = True
                    self.stop.clear()
                    self.refresh_start_position(tracker)
                    self.navigation.travel_turn_rate=turn_rate
                    self.navigation.road_half_width=self.navigation_width()
                    if not hasattr(self.navigation,'travel_speed'):
                        self.navigation.travel_speed=3.5 if (self.nav_packet or {}).get('stealth') else 7.
                self.smooth_path()
                if self.addon_map_id and self.planned is None:
                    raise NavigationError(self.status.get())
            follower = None if measure else (Follower(self.points) if movement else (Follower(self.points) if len(self.points)>1 else None))
            if follower and isinstance(tracker, AddonTracker):
                if movement:
                    if not self.planned or not self.navigation:
                        raise NavigationError(self.status.get())
                    follower=CurveFollower(self.planned.pixels,prepared=True,
                                           yards_per_pixel=self.navigation.transform.yards_per_pixel)
                    follower.navigation=self.navigation
                    follower.nav_world=self.planned.world[0]
                    follower.nav_route=self.planned.world
                    follower.target_guard=lambda pixel:self.navigation.target_clear(follower.nav_world,pixel)
                    follower.tolerance=min(.35,1./self.navigation.transform.yards_per_pixel)
                else:
                    follower=None  # Preview never needs an executable route.
        except Exception as error:
            messagebox.showerror('Not ready', str(error))
            return
        if focused and (self.stop.is_set() or stop_hotkey_pressed() or not game_focused()):
            self.status.set('Cancelled or World of Warcraft lost focus.')
            return
        if not focused and not self.focus_for_action():
            return
        self.stop.clear()
        self.persist_calibration()
        self.current = None
        self.badge.configure(text='MOVEMENT' if movement else 'PREVIEW', foreground='#a63b32' if movement else '#236a93')
        for button in self.buttons + [self.preview_button, self.run_button]:
            button.configure(state='disabled')
        if measure or timed:
            path = route_curve(self.points) if self.addon_map_id else list(self.points)
            self.thread = threading.Thread(target=self.work_timed, args=(tracker, path, speed, turn_rate, measure, .5 if self.stealth.get() else 1.), daemon=True)
        else:
            self.thread = threading.Thread(target=self.work, args=(movement, tracker, follower, stall_timeout, turn_rate), daemon=True)
        self.thread.start()

    def start_continent_route(self, from_overlay=False):
        if self.busy(): return
        try:
            if not self.connection: raise NavigationError('Connect the addon before choosing a destination.')
            target=dict(self.world_destination)
            if target['continent'] not in self.atlas.continents or target['zone'] not in self.atlas.zones:
                raise NavigationError('Select a supported destination zone.')
            if self.atlas.zones[target['zone']]['continent']!=target['continent']:
                raise NavigationError('Destination zone and continent do not match.')
            if len(target['world'])!=3 or not all(math.isfinite(v) for v in target['world']):
                raise NavigationError('Choose a finite continent destination.')
            if self.timed_mode.get(): raise NavigationError('Continent routes require live feedback. Turn off Timed route.')
            turn_rate=float(self.turn_rate.get())
            if not math.isfinite(turn_rate) or turn_rate<=0: raise ValueError('Turn rate must be positive and finite.')
            road_half_width=self.navigation_width()
        except (ValueError,TypeError,KeyError,tk.TclError) as error:
            if from_overlay: self.status.set(f'Overlay route not started: {error}')
            else: messagebox.showerror('Not ready',str(error))
            return
        if from_overlay:
            if not game_focused() or stop_hotkey_pressed(): return
        elif not self.focus_for_action(): return
        self._overlay_route=from_overlay
        self.region=self.connection['region']
        self.stop.clear();self.preparing=True
        self.status.set('Refreshing position and checking the connected continent road network…')
        self.badge.configure(text='PLANNING',foreground='#236a93')
        for button in self.buttons+[self.preview_button,self.run_button]: button.configure(state='disabled')
        self.thread=threading.Thread(target=self.prepare_continent_route,
            args=(target,turn_rate,road_half_width),daemon=True)
        self.thread.start()

    def navigation_width(self):
        width=float(self.road_half_width.get())
        if not math.isfinite(width) or not .1<=width<=3:
            raise ValueError('Road half-width must be between 0.1 and 3 yards.')
        return width

    def prepare_continent_route(self, target, turn_rate, road_half_width=.4):
        try:
            continent=target['continent']
            tracker=ContinentTracker(self.atlas,continent,(0.,0.))
            self.timed_observation(tracker)
            roads,owners=self.atlas.roads(self.road_store,continent)
            source,_=self.atlas.packet_zone(tracker.packet)
            if source not in owners or target['zone'] not in owners:
                raise NavigationError('Save roads in both the starting and destination zones before routing between them.')
            navigation=ContinentNavigation.load_roads(self.atlas,continent,roads,tracker.packet['world'],target['world'],self.stop)
            # Tile loading may take seconds; plan from a new heartbeat afterward.
            self.timed_observation(tracker)
            navigation.travel_turn_rate=turn_rate
            navigation.road_half_width=road_half_width
            navigation.travel_speed=getattr(getattr(self,'navigation',None),'travel_speed',3.5 if tracker.packet.get('stealth') else 7.)
            journey=plan_continent_route(roads,owners,navigation,target['world'],target['zone'],tracker.packet,self.stop)
            if self.stop.is_set() or stop_hotkey_pressed() or not game_focused():
                raise NavigationError('Cancelled or World of Warcraft lost focus during planning.')
            self.emit('continent_ready',navigation,tracker,journey,target,turn_rate)
        except Exception as error:
            try:
                record_route_error(error,roads if 'roads' in locals() else [],
                    dict((getattr(locals().get('tracker'),'last_decoded_packet',None) or {}),destination=target,
                         planning_report=getattr(locals().get('navigation'),'planning_report',{})))
            except (OSError,ValueError,TypeError): pass
            self.emit('done',f'Route not started: {error}')

    def apply_continent_ready(self, navigation, tracker, journey, target, turn_rate):
        self.preparing=False
        if self.stop.is_set() or stop_hotkey_pressed() or not game_focused():
            self.emit('done','Route cancelled before movement.');return
        self.navigation=navigation;self.nav_packet=tracker.packet;self.nav_error=''
        self.connection.update(packet=tracker.packet,zone_id=tracker.zone_id,map_id=tracker.map_id,navigation=navigation)
        self.zone_id=tracker.zone_id;self.addon_map_id=tracker.map_id
        self.map_name=self.atlas.continents[target['continent']]['name']
        image=self.atlas.image(target['continent'])
        self.reference=self.display=self.plan_image=image
        self.live_bounds=self.plan_bounds=(0,0,image.shape[1],image.shape[0])
        self.view='plan';self.points=list(journey.route.requested)
        self.planned=journey.route;self.road_journey=journey
        self.road_destination=None;self.world_destination=target
        self.center=journey.route.pixels[0]
        facing=tracker.packet['facing']
        self.tip=(self.center[0]+15*math.sin(math.radians(facing)),self.center[1]-15*math.cos(math.radians(facing)))
        self.current=(self.center,facing,1.)
        follower=CurveFollower(journey.route.pixels,prepared=True,yards_per_pixel=navigation.transform.yards_per_pixel)
        follower.navigation=navigation;follower.nav_world=journey.route.world[0];follower.nav_route=journey.route.world
        follower.target_guard=lambda p:navigation.target_clear(follower.nav_world,p)
        follower.tolerance=min(.35,1./navigation.transform.yards_per_pixel)
        self.atlas_browser.show_continent(target['continent'],traveling=True)
        self.badge.configure(text='MOVEMENT',foreground='#a63b32')
        self.nav_label.configure(text=f'{len(navigation.tiles)} ground tiles · '+('mixed coverage' if self.catalog.profile else 'continent route'))
        self.status.set(f'Continent journey · {journey.road_yards:.0f} yards on saved roads.'+
                        (' Updated zones have no obstacle checks.' if self.catalog.profile else ' Ground checked.'))
        self.persist_calibration()
        self.thread=threading.Thread(target=self.work,args=(True,tracker,follower,5,turn_rate),daemon=True)
        self.thread.start()

    def refresh_start_position(self, tracker):
        """Read a live heartbeat before planning, without resetting the trip or roads."""
        # A new run must infer its own ground surface; the preceding trip's
        # altitude/position cannot justify a different floor after manual travel.
        _, found = self.timed_observation(tracker)
        self.navigation.validate_packet(tracker.packet)
        self.nav_packet = tracker.packet
        if self.connection: self.connection['packet']=tracker.packet
        self.center, facing, _ = found
        self.tip = (self.center[0]+15*math.sin(math.radians(facing)),
                    self.center[1]-15*math.cos(math.radians(facing)))
        self.current = found
        self.planned = self.road_journey = None
        self.render()

    def timed_observation(self, tracker):
        if self.stop.is_set() or stop_hotkey_pressed() or not game_focused():
            raise ValueError('Cancelled or World of Warcraft lost focus.')
        rgb = np.asarray(ImageGrab.grab(bbox=self.region, all_screens=True).convert('RGB'))
        if isinstance(tracker, AddonTracker):
            found = tracker.locate(rgb)
            sequence = tracker.sequence
            deadline = time.monotonic()+1
            while tracker.sequence == sequence:
                if self.stop.wait(.06) or stop_hotkey_pressed() or not game_focused():
                    raise ValueError('Cancelled or World of Warcraft lost focus.')
                if time.monotonic()>deadline:
                    raise ValueError('Addon data stopped updating.')
                found = tracker.locate(np.asarray(ImageGrab.grab(bbox=self.region, all_screens=True).convert('RGB')))
            return self.reference, found
        if not title_unchanged(self.reference, rgb, self.title_bounds):
            raise ValueError('Zone title changed or map moved. Find current zone again.')
        found = tracker.locate(rgb)
        if found is None:
            raise ValueError('Starting arrow is uncertain. Recalibrate.')
        return rgb, found

    def work_timed(self, tracker, points, speed, turn_rate, measure=False, speed_multiplier=1.):
        keyboard = Keyboard()
        reason = 'Stopped.'
        try:
            if self.stop.is_set() or stop_hotkey_pressed():
                return
            rgb, found = self.timed_observation(tracker)
            position, facing, score = found
            if isinstance(tracker, AddonTracker):
                speed_multiplier = .5 if tracker.packet['stealth'] else 1.
            if measure == 'turn':
                samples = []
                for duration in (.25,.5):
                    self.emit('status', f'Measuring turn rate: turning right for {duration:g}s while stationary. Backspace / Esc cancel.')
                    before = time.monotonic()
                    if not keyboard.pulse('right', duration, self.stop):
                        reason = 'Turn measurement cancelled.'
                        return
                    elapsed = time.monotonic()-before
                    rgb, final = self.timed_observation(tracker)
                    change = angle_error(final[1], facing)
                    if not 5<=change<=150 or distance(final[0],position)>1.5:
                        raise ValueError('Turn measurement was unclear. Stand still; bind Right Arrow to turn right, not strafe.')
                    samples.append((change,elapsed))
                    facing = final[1]
                rates = [change/elapsed for change,elapsed in samples]
                if max(rates)/min(rates)>1.25:
                    raise ValueError('Turn samples disagree. Keep the game responsive and try again.')
                measured = sum(change for change,_ in samples)/sum(elapsed for _,elapsed in samples)
                self.emit('turn_rate', measured, rgb, final)
                reason = f'Turn rate measured: {measured:.2f}°/s. Saved for future runs.'
                return
            if measure:
                self.emit('status', 'Measuring speed: holding W for 6 seconds. Keep a clear straight path; Backspace / Esc cancel.')
                before = time.monotonic()
                if not keyboard.pulse('forward', 6, self.stop):
                    reason = 'Speed measurement cancelled.'
                    return
                elapsed = time.monotonic()-before
                rgb, final = self.timed_observation(tracker)
                if isinstance(tracker, AddonTracker) and tracker.packet['stealth'] != (speed_multiplier==.5):
                    raise ValueError('Stealth changed during measurement. Keep one movement state and try again.')
                displacement = distance(position, final[0])
                minimum_displacement = .15 if isinstance(tracker, AddonTracker) else 3
                if displacement < minimum_displacement or abs(angle_error(final[1], facing)) > 20:
                    raise ValueError('Too little straight movement to measure speed. Try clear ground at a larger map scale.')
                if abs(angle_error(heading(position, final[0]), facing)) > 25:
                    raise ValueError('Measured motion disagrees with facing. Check arrow calibration.')
                normal_speed = displacement/elapsed/speed_multiplier
                self.emit('speed', normal_speed, rgb, final)
                reason = f'Normal speed: {normal_speed:.3f} map px/s (measured at {speed_multiplier:.0%} speed). Draw a new route from the updated marker.'
                return
            commands = timed_route(points, position, facing, speed*speed_multiplier, turn_rate)
            total = sum(seconds for _, seconds in commands)
            self.emit('frame', rgb, found, f'Timed route: {total:.1f}s planned. This marker is the starting position only.')
            # From this point onward no screenshots or position checks occur.
            for index, (key, seconds) in enumerate(commands):
                if self.stop.is_set():
                    return
                self.emit('status', f'TIMED · {key} for {seconds:.2f}s · command {index+1}/{len(commands)} · position is not being checked')
                if not keyboard.pulse(key, seconds, self.stop):
                    reason = 'Stopped: Backspace, Esc, stop request, or game focus changed.'
                    return
            reason = 'Timed route finished; keys released. Destination was not verified.'
        except Exception as error:
            reason = f'Stopped: {error}'
        finally:
            try:
                keyboard.close()
            finally:
                self.emit('done', reason)

    def apply_speed(self, speed, rgb, found):
        self.update_position(rgb, found)
        self.points = []
        self.move_speed.set(f'{speed:.3f}')
        self.persist_calibration()
        self.render()

    def apply_turn_rate(self, rate, rgb, found):
        self.update_position(rgb, found)
        self.turn_rate.set(round(rate,2))
        self.persist_calibration()
        self.render()

    def update_position(self, rgb, found):
        position, facing, _ = found
        rotation = math.radians(angle_error(facing, heading(self.center, self.tip)))
        def rotate(point):
            dx, dy = point[0]-self.center[0], point[1]-self.center[1]
            return (position[0]+dx*math.cos(rotation)-dy*math.sin(rotation),
                    position[1]+dx*math.sin(rotation)+dy*math.cos(rotation))
        self.tip, self.color_sample = rotate(self.tip), rotate(self.color_sample or self.center)
        self.center = position
        self.reference = self.display = rgb
        self.current = found

    def emit(self, kind, *values):
        item = (kind, values)
        try:
            self.events.put_nowait(item)
        except queue.Full:
            try:
                self.events.get_nowait()
            except queue.Empty:
                pass
            self.events.put_nowait(item)

    def work(self, movement, tracker, follower, stall_timeout=20, turn_rate=180.):
        keyboard = Keyboard()
        movement_record=None
        reason = 'Stopped.'
        try:
            if self.stop.is_set() or stop_hotkey_pressed():
                return
            baseline = cv2.GaussianBlur(self.reference, (21, 21), 0).astype(np.float32)
            addon = isinstance(tracker, AddonTracker)
            navigation = follower.navigation if isinstance(follower,CurveFollower) and hasattr(follower,'navigation') else None
            if navigation:
                stall_timeout=min(stall_timeout,5)
                keyboard.feedback_lease=.45
                if movement:
                    movement_record=MovementRecord()
                    motion_history=MotionHistory()
                    def movement_key(key,down):
                        motion_history.key(key,down)
                        movement_record.key(key,down)
                    keyboard.event_sink=movement_key
            keyboard.pause_on_focus_loss = addon
            resume_anchor = None
            ground_anchor = None
            progress = ProgressMonitor(timeout=stall_timeout, minimum_distance=.15 if addon else 1.5)
            turns = TurnTiming(rate=turn_rate, precise=addon)
            timing_noise = TimingNoise()
            steering=SteeringPolicy(timing_noise)
            speed_estimate=SpeedEstimate(3.5 if addon and tracker.packet and tracker.packet.get('stealth') else 7.)
            jumps=JumpSchedule(time.monotonic(),noise=timing_noise)
            previous_sequence = None
            stable = 0
            failures = 0
            last_facing = None
            turn_started = None
            first = True
            while not self.stop.is_set():
                if stop_hotkey_pressed():
                    reason = 'Stopped with Backspace or Esc.'
                    break
                if addon and (keyboard.focus_paused.is_set() or not game_focused()):
                    keyboard.release()
                    ground_anchor = None
                    speed_estimate.reset()
                    keyboard.focus_paused.set()
                    self.emit('paused', 'Paused while WoW is in the background. Return to WoW to resume; Backspace/Esc cancels.')
                    while not game_focused():
                        if self.stop.wait(.05) or stop_hotkey_pressed():
                            reason = 'Stopped while paused.'
                            return
                    if self.stop.is_set() or stop_hotkey_pressed():
                        return
                    resume_anchor = tracker.last
                    stable, failures, last_facing, turn_started = 0,0,None,None
                    previous_sequence = None
                    tracker.sequence = None
                    tracker.changed = time.monotonic()
                    turns.previous = None
                    steering=SteeringPolicy(timing_noise)
                    jumps=JumpSchedule(time.monotonic(),noise=timing_noise)
                    if follower:
                        follower.aligning = True
                    progress = ProgressMonitor(timeout=stall_timeout, minimum_distance=.15)
                    keyboard.focus_paused.clear()
                    self.emit('resuming', 'WoW is active. Checking fresh position and heading before resuming…')
                if not game_focused():
                    if addon:
                        continue
                    reason = 'Stopped: World of Warcraft lost foreground focus.'
                    break
                capture_started=time.monotonic()
                rgb = np.asarray(ImageGrab.grab(bbox=self.region, all_screens=True).convert('RGB'))
                capture_finished=time.monotonic()
                observed_at=(capture_started+capture_finished)/2
                if addon and not game_focused():
                    keyboard.release()
                    ground_anchor = None
                    keyboard.focus_paused.set()
                    continue
                if not isinstance(tracker, AddonTracker) and not title_unchanged(self.reference, rgb, getattr(self, 'title_bounds', None)):
                    reason = 'Stopped: the zone title changed or the map moved. Find current zone again.'
                    break
                captured = rgb
                if isinstance(tracker, AddonTracker):
                    rgb = self.reference
                changed = 0
                if not isinstance(tracker, AddonTracker):
                    blurred = cv2.GaussianBlur(rgb, (21, 21), 0).astype(np.float32)
                    changed = np.mean(np.mean(np.abs(blurred-baseline), axis=2) > 28)
                # Revealing terrain is expected. This guard only rejects broad scene
                # replacement; it does not establish zone identity or detect every pan.
                if changed > .60:
                    reason = 'Stopped: map appearance changed. Restore the map or recapture and recalibrate.'
                    break
                found = tracker.locate(captured)
                if addon:
                    if tracker.sequence == previous_sequence:
                        # Do not act twice on one packet, or extend a forward
                        # hold's watchdog lease with an unchanged screenshot.
                        self.stop.wait(.02)
                        continue
                    previous_sequence = tracker.sequence
                if found is None:
                    ground_anchor = None
                    speed_estimate.reset()
                    if movement:
                        keyboard.release()
                        steering=SteeringPolicy(timing_noise)
                        progress.update(tracker.last, time.monotonic(), False)
                    stable = 0
                    failures += 1
                    self.emit('frame', rgb, None, 'Tracking uncertain: movement withheld.')
                    if failures >= 4:
                        reason = 'Stopped: arrow tracking lost or ambiguous. Recalibrate.'
                        break
                    self.stop.wait(.15)
                    continue
                position, facing, score = found
                if navigation:
                    world,alignment=navigation.validate_packet(tracker.packet,previous=ground_anchor)
                    if first and distance(world,follower.nav_route[0])>1.5:
                        raise NavigationError('Player moved since planning. Connect and redraw from the current position.')
                    if not first and distance(world,follower.nav_world)>5:
                        raise NavigationError('Unexpected world-position jump. Movement stopped.')
                    previous_world=follower.nav_route[0] if first else follower.nav_world
                    if not navigation.engine.transition_clear(previous_world,world):
                        raise NavigationError('Unverified ground-surface transition. Reconnect and redraw on the current level.')
                    ground_anchor=world
                    follower.nav_world=world
                    follower.locate_progress(position)
                    if follower.route_distance*navigation.transform.yards_per_pixel>2:
                        keyboard.release()
                        progress.update(position,time.monotonic(),False)
                        follower=recover_route(navigation,follower,world)
                        stable=0;turn_started=None
                        steering=SteeringPolicy(timing_noise)
                        jumps=JumpSchedule(time.monotonic(),noise=timing_noise)
                        self.emit('status','Correcting drift: following a checked return to your route.')
                if resume_anchor is not None:
                    if distance(position,resume_anchor)>5:
                        reason = 'Stopped: character moved away from the paused position. Reconnect and update the route.'
                        break
                    resume_anchor = None
                # Continuous holds can span captures, so one packet's rotation
                # is not the response to one completed pulse. Use calibration.
                if not navigation:
                    turns.observe(facing)
                failures = 0
                if first and distance(position, self.center) > 12:
                    reason = 'Stopped: player is away from the calibrated starting point. Recapture.'
                    break
                first = False
                stable += 1
                if navigation:
                    speed=speed_estimate.observe(world,observed_at,keyboard.is_held('forward'))
                    try:
                        command=decide(navigation,follower,follower.nav_world,position,facing,speed,turns.rate,
                                       keyboard.is_held('forward'),steering,time.monotonic(),
                                       motion_history=motion_history if movement else None,observed_at=observed_at)
                    except SteeringTargetError:
                        keyboard.release()
                        progress.update(position,time.monotonic(),False)
                        follower=recover_route(navigation,follower,follower.nav_world)
                        stable=0;turn_started=None
                        steering=SteeringPolicy(timing_noise)
                        jumps=JumpSchedule(time.monotonic(),noise=timing_noise)
                        self.emit('status','Correcting obstacle approach: following a checked return to your route.')
                        self.stop.wait(.02)
                        continue
                    action,error,duration=command.action,command.error,command.seconds
                    keyboard.feedback_lease=command.lease
                else:
                    action,error=follower.step(position,facing) if follower else ('observe',0)
                    if addon and movement and follower:
                        action=follower.steering(position,facing,action,error,keyboard.is_held('forward'))
                    duration=.10 if action.startswith('strafe_') else turns.duration(error)
                    if addon: duration=timing_noise.scale(duration,.06)
                strafing=action.startswith('strafe_')
                curved=action.startswith('forward_')
                description = f'{"LIVE" if movement else "PREVIEW"} · match {score:.2f} · facing {facing:.0f}° · {action} · error {error:+.0f}°'
                projection = self.projection()
                if projection:
                    zx, zy = projection.zone_coordinates(position)
                    description += f' · estimated coordinates {zx:.1f}, {zy:.1f}'
                if follower:
                    description += f' · waypoint {min(follower.index+1, len(follower.points))}/{len(follower.points)}'
                if navigation:
                    description += f' · {"ground checked" if navigation.collision_checked(world) else "roads only"} · alignment {alignment:.2f} yd'
                    description += f' · speed {speed:.1f} yd/s'
                    if isinstance(tracker,ContinentTracker): description += ' · '+tracker.packet['name']
                self.emit('frame', rgb, found, description, tracker.packet if addon else None, capture_finished)
                if movement:
                    if action == 'arrived':
                        reason = f'Arrived within {follower.tolerance:g} map pixels of the destination. Movement stopped.'
                        break
                    if action in ('left', 'right', 'wait'):
                        if turn_started is None:
                            turn_started = time.monotonic()
                        if time.monotonic()-turn_started > 10:
                            reason = ('Stopped: no clear movement from the current ground. Reconnect and update the route.'
                                      if action=='wait' else 'Stopped: unable to establish the requested heading. Check turn bindings and calibration.')
                            break
                    else:
                        turn_started = None
                    if progress.update(position, time.monotonic(), keyboard.is_held('forward')):
                        reason = f'Stopped: no measurable progress during {stall_timeout:g}s of forward movement.'
                        break
                    if stable >= 3:
                        if last_facing is not None and abs((facing-last_facing+180)%360-180) > 70:
                            reason = 'Stopped: implausible heading change. Check arrow tracking.'
                            break
                        if navigation:
                            if action=='wait':
                                keyboard.release()
                                active=not self.stop.is_set()
                            else:
                                active=keyboard.command(command.keys,duration,self.stop,forward_seconds=command.forward_seconds)
                            now=time.monotonic()
                            if active: steering.applied(command,now)
                            eligible=(active and action=='forward' and abs(error)<3 and
                                      abs(follower.cross_track)*navigation.transform.yards_per_pixel<.35)
                            if eligible and now>=jumps.next_at:
                                eligible=navigation.jump_clear(follower.nav_world,facing,speed,follower)
                            jump_seconds=jumps.consider(now,eligible)
                            if jump_seconds is not None:
                                keyboard.jump(jump_seconds,self.stop)
                        elif action == 'forward':
                            active = keyboard.hold_forward(self.stop)
                        else:
                            keys = [part.removeprefix('forward_') for part in action.split('+')]
                            key = keys[0]
                            if strafing or curved:
                                active = keyboard.nudge(keys,duration,self.stop)
                            else:
                                active = keyboard.pulse(key,duration,self.stop)
                            if active and not strafing and not navigation:
                                turns.record(facing,key,keyboard.last_duration)
                        if not active:
                            if addon and keyboard.focus_paused.is_set() and not self.stop.is_set():
                                continue
                            reason = keyboard.stop_reason or 'Stopped: emergency stop or game focus changed.'
                            break
                        progress.previous_moving = keyboard.is_held('forward')
                        last_facing = facing
                        if movement_record:
                            movement_record.frame(world=follower.nav_world,facing=facing,action=action,
                                                  error=error,offset_yards=follower.route_distance*navigation.transform.yards_per_pixel,
                                                  seconds=duration,lease=keyboard.feedback_lease,held=sorted(keyboard.held),
                                                  speed_yards_per_second=speed,
                                                  forward_seconds=command.forward_seconds,
                                                  capture_seconds=capture_finished-capture_started,
                                                  prediction_seconds=follower.prediction_seconds,
                                                  predicted_world=follower.predicted_world,
                                                  predicted_facing=follower.predicted_facing,
                                                  comfort_width_yards=follower.comfort_width,
                                                  decision_reason=command.reason)
                self.stop.wait(timing_noise.poll(command.lease,time.monotonic()-capture_started) if navigation
                               else timing_noise.between(.07,.09) if addon else .08)
        except Exception as error:
            reason = f'Stopped due to error: {error}'
        finally:
            try:
                keyboard.close()
            finally:
                if movement_record:
                    if reason.startswith('Stopped due to error:'):
                        movement_record.frame(event='error',previous_world=ground_anchor,
                                              packet=getattr(tracker,'last_decoded_packet',None) or tracker.packet,reason=reason)
                    try: movement_record.finish(keyboard.stop_reason or reason)
                    except (OSError,ValueError,TypeError): pass
                self.emit('done', keyboard.stop_reason or reason)

    def halt(self):
        if not self.busy() and self.drawing and self.mode in ('roads','route'):
            self.drawing=False;self.path_cursor=None;self.road_draft=[]
            if self.mode=='route': self.points=[];self.planned=None
            self.status.set('Unfinished path cancelled.');self.render();return
        self.stop.set()
        self.status.set('Stopping and releasing movement keys…' if self.busy() else 'Stopped.')

    def poll(self):
        try:
            while True:
                kind, values = self.events.get_nowait()
                if kind == 'status':
                    self.status.set(values[0])
                elif kind == 'paused':
                    self.status.set(values[0])
                    self.badge.configure(text='PAUSED',foreground='#a66b00')
                elif kind == 'resuming':
                    self.status.set(values[0])
                    self.badge.configure(text='TRACKING',foreground='#236a93')
                elif kind == 'speed':
                    self.apply_speed(*values)
                elif kind == 'turn_rate':
                    self.apply_turn_rate(*values)
                elif kind == 'addon':
                    self.apply_addon(*values)
                elif kind == 'continent_ready':
                    self.apply_continent_ready(*values)
                elif kind == 'classic_preview':
                    self.navigation,self.planned=values
                    self.nav_label.configure(text=f'{len(self.navigation.tiles)} Classic ground tiles')
                    self.render()
                elif kind == 'acquired':
                    self.apply_acquisition(*values)
                elif kind == 'frame':
                    self.display, self.current, description = values[:3]
                    if len(values)>3 and values[3]:
                        self._overlay_pose.update(values[3],values[4] if len(values)>4 else time.monotonic())
                        self.nav_packet=values[3]
                        if self.connection:
                            self.connection['packet']=values[3]
                            try:
                                zone_id,_=self.atlas.packet_zone(values[3])
                                self.connection.update(zone_id=zone_id,map_id=values[3]['map_id'])
                            except NavigationError: pass
                    self.status.set(description)
                    self.render()
                elif kind == 'done':
                    self.preparing=False
                    self.status.set(values[0])
                    self.badge.configure(text='STOPPED', foreground='#a63b32')
                    for button in self.buttons + [self.preview_button, self.run_button]:
                        button.configure(state='normal')
                    self.zone_picker.configure(state='readonly')
                    self.road_picker.configure(state='readonly')
                    self.atlas_browser.picker.configure(state='readonly')
                    if hasattr(self,'map_version_picker'):
                        self.map_version_picker.configure(state='readonly')
                    if self.reconnect_route is None and self.addon_map_id and self.points and not self.planned and not self.busy():
                        self.smooth_path()
                    # Return to the map after completing an action, unless the user
                    # deliberately switched to another application while it ran.
                    if game_focused() and not self._overlay_route:
                        self.root.deiconify()
                        self.root.lift()
                        self.root.focus_force()
                    self._overlay_route=False
        except queue.Empty:
            pass
        if not self._closing:
            self.poll_overlay()
        self.root.after(70, self.poll)

    def close(self):
        if not self._closing:
            self._closing=True
            self.overlay_reader.close()
            if self.overlay: self.overlay.close();self.overlay=None
        self.stop.set()
        if self.busy():
            self.root.after(50, self.close)
        else:
            self.root.destroy()


if __name__ == '__main__':
    import argparse
    parser=argparse.ArgumentParser()
    parser.add_argument('--smoke-test',action='store_true')
    parser.add_argument('--map-profile',help='Directory of a client map profile; updated zones follow roads without collision, other zones use legacy collision.')
    args=parser.parse_args()
    try:
        catalog=MapCatalog(args.map_profile) if args.map_profile else startup_catalog()
    except (OSError, ValueError) as error:
        parser.error(str(error))
    if args.map_profile and not catalog.profile:
        parser.error('--map-profile requires a completed client profile with profile.json.')
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except (OSError, AttributeError):
        pass
    root = tk.Tk()
    if args.smoke_test:
        root.withdraw()
        app = App(root,catalog)
        root.update()
        root.destroy()
        print('Tk application initialized successfully; no capture or input performed.')
    else:
        app = App(root,catalog)
        if catalog and catalog.profile and '44' in catalog.profile['zones']:
            app.road_zone.set(catalog.by_id[44]['name'])
            app.open_road_map()
            app.status.set(f'{map_profile_label(catalog)}. Connect to start routing.')
        root.mainloop()
