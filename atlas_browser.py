"""WoW-style world/continent/zone browsing and shared-coordinate overlays."""
import math
import tkinter as tk
from tkinter import ttk

from PIL import Image, ImageTk

from navigation import NavigationError


class AtlasBrowser:
    def __init__(self, app):
        self.app=app;self.atlas=app.atlas;self.level=None;self.continent=0
        self.view_zone=None
        self.roads=[];self.owners=[];self.road_continent=None

    @property
    def active(self): return self.level is not None

    def attach(self):
        app=self.app
        bar=ttk.Frame(app.root,padding=(12,0,12,6));bar.pack(fill='x',before=app.map_tools)
        for label,command in [('World',self.show_world),('← Continent',self.show_continent),('Player zone',self.player_zone)]:
            button=ttk.Button(bar,text=label,command=command);button.pack(side='left',padx=(0,4))
        self.choice=tk.StringVar(value='Eastern Kingdoms')
        self.picker=ttk.Combobox(bar,textvariable=self.choice,state='readonly',width=20)
        self.picker.pack(side='left',padx=8)
        self.update_picker()
        self.picker.bind('<<ComboboxSelected>>',self.pick_continent)
        browse=ttk.Radiobutton(bar,text='Browse maps',variable=app.routing_mode,value='browse',command=app.set_routing_mode)
        browse.pack(side='left',padx=8);app.buttons.append(browse)

    def update_picker(self):
        self.picker.configure(values=[self.atlas.continents[key]['name'] for key in sorted(self.atlas.continents)])
        self.choice.set(self.atlas.continents[self.continent]['name'])

    def pick_continent(self, event):
        name=self.choice.get()
        self.show_continent(next(key for key,record in self.atlas.continents.items() if record['name']==name))

    def finish_edit(self):
        app=self.app
        if app.pen.draft is not None and len(app.pen.draft)>=2 and not app.pen.finish(): return False
        app.pen.cancel();app.drawing=False;app.road_draft=[]
        app.cancel_destination_click()
        return True

    def show_world(self):
        if not self.app.busy() and not self.finish_edit(): return
        self.level='world';self.app.viewport.reset()
        if not self.app.busy(): self.app.set_routing_mode()
        self.app.map_label.configure(text='World · choose a continent')
        self.app.render()

    def show_continent(self, continent=None, traveling=False):
        app=self.app
        show_parent=continent is None
        if show_parent:
            zone=self.atlas.zones.get(app.zone_id)
            continent=zone['continent'] if zone and self.level is None else self.continent
        region=self.atlas.continents[continent]
        if 'zone_id' in region:
            if show_parent:
                self.show_world()
            else:
                self.open_zone(region['zone_id'])
            return
        if not traveling and not app.busy() and not self.finish_edit(): return
        self.continent=continent;self.level='continent'
        self.choice.set(region['name'])
        self.load_roads(continent)
        app.viewport.reset()
        if not traveling and not app.busy(): app.set_routing_mode()
        app.map_label.configure(text=self.atlas.continents[continent]['name']+' · continent')
        app.render()

    def open_zone(self, zone_id, mode=None):
        app=self.app
        if zone_id is None: return
        continent=self.atlas.zones[zone_id]['continent']
        if app.busy():
            # During planning/travel only the displayed map changes. The
            # worker retains its capture, projection, ground and route data.
            self.continent=continent
            self.choice.set(self.atlas.continents[continent]['name'])
            if self.road_continent!=self.continent: self.load_roads(self.continent)
            self.view_zone=zone_id;self.level='zone'
            app.viewport.reset()
            app.map_label.configure(text=self.atlas.zones[zone_id]['name']+' · route view')
            app.render();return
        mode=mode or app.routing_mode.get()
        if not self.finish_edit(): return
        self.continent=continent
        self.choice.set(self.atlas.continents[continent]['name'])
        self.level=None;app.road_zone.set(self.atlas.zones[zone_id]['name'])
        app.open_road_map()
        app.routing_mode.set(mode);app.set_routing_mode()

    def player_zone(self):
        if self.app.connection:
            self.open_zone(self.app.connection['zone_id'])
        else: self.app.status.set('Connect to locate your player first.')

    def surface(self):
        if self.level=='zone': return self.atlas.catalog.image(self.view_zone)
        return self.atlas.image(None if self.level=='world' else self.continent)

    def load_roads(self, continent):
        try: self.roads,self.owners=self.atlas.roads(self.app.road_store,continent)
        except (ValueError,OSError) as error:
            self.roads=[];self.owners=[];self.app.status.set(f'Road network needs attention: {error}')
        self.road_continent=continent

    def transform(self):
        return self.atlas.transform(self.view_zone) if self.level=='zone' else self.atlas.frame(self.continent)

    def zoom(self, factor, x=None, y=None):
        app=self.app
        if factor<1 and app.viewport.zoom<=1.0001:
            if self.level=='continent': self.show_world();return True
            if self.level=='zone': self.show_continent();return True
            if self.level is None and app.zone_id in self.atlas.zones:
                self.show_continent();return True
        if factor>1 and self.level in ('world','continent'):
            p=app.viewport.point(x if x is not None else app.canvas.winfo_width()/2,
                                 y if y is not None else app.canvas.winfo_height()/2)
            h,w=self.surface().shape[:2]
            if not (0<=p[0]<=w and 0<=p[1]<=h): return False
            if self.level=='world':
                continent=self.atlas.world_continent_at(p)
                if continent is not None:
                    self.show_continent(continent);return True
                return False
            # Scrolling over a zone drills into its map; +/- still magnifies
            # the overview so both levels remain available at larger scales.
            if x is not None and y is not None:
                zone=self.atlas.zone_at(self.continent,self.atlas.frame(self.continent).world(p,0.))
                if zone is not None:
                    self.open_zone(zone);return True
        return False

    def down(self, event):
        app=self.app;p=app.coordinates(event)
        if p is None: return
        if self.level=='zone':
            if app.mode in ('roads','pen','route') and not app.busy():
                # Reopen the editable map after travel, preserving the view
                # used to select this click. Destination clicks use world XY.
                zoom,pan=app.viewport.zoom,app.viewport.pan
                self.open_zone(self.view_zone)
                if self.level=='zone': return
                app.viewport.zoom,app.viewport.pan=zoom,pan
                app.render();app.down(event)
            return
        if self.level=='world':
            continent=self.atlas.world_continent_at(p)
            if continent is not None: self.show_continent(continent)
            return
        world=self.atlas.frame(self.continent).world(p,0.)
        zone=self.atlas.zone_at(self.continent,world)
        if zone is None:
            app.status.set('Choose a point inside a supported outdoor zone.');return
        if app.mode in ('browse','roads','pen','route') or getattr(event,'state',0)&1:
            self.open_zone(zone);return
        app.status.set('Open a zone to edit roads, or choose Click destination to travel.')

    def choose_destination(self, event):
        app=self.app;p=app.coordinates(event)
        if p is None or self.level not in ('continent','zone'): return False
        world=self.transform().world(p,0.)
        zone=self.view_zone if self.level=='zone' else self.atlas.zone_at(self.continent,world)
        if zone is None:
            app.status.set('Choose a point inside a supported outdoor zone.');return False
        if not app.connection:
            app.status.set('Connect the addon before choosing a destination.');return False
        app.world_destination=dict(continent=self.continent,world=world,zone=zone)
        app.planned=app.road_journey=app.road_destination=None;app.points=[]
        app.render();return True

    def render(self):
        app=self.app;surface=self.surface();h,w=surface.shape[:2]
        cw,ch=max(1,app.canvas.winfo_width()),max(1,app.canvas.winfo_height())
        app.viewport.layout(cw,ch,w,h);app.scale,app.offset=app.viewport.scale,app.viewport.offset
        affine=(1/app.scale,0,-app.offset[0]/app.scale,0,1/app.scale,-app.offset[1]/app.scale)
        visible=Image.fromarray(surface).transform((cw,ch),Image.Transform.AFFINE,affine,
            resample=Image.Resampling.BILINEAR,fillcolor='#17212b')
        app.photo=ImageTk.PhotoImage(visible);app.canvas.create_image(0,0,anchor='nw',image=app.photo)
        app.zoom_label.configure(text=f'{app.viewport.zoom:.0%} · Wheel zooms maps · Right-drag pans')
        if self.level=='world': return
        t=self.transform()
        def xy(world):
            p=t.pixel(world);return p[0]*app.scale+app.offset[0],p[1]*app.scale+app.offset[1]
        for road in self.roads:
            if len(road)>1: app.canvas.create_line(*[v for p in road for v in xy(p)],fill='#7bb8f2',width=3)
        if app.planned and app.navigation and app.navigation.transform.continent==self.continent:
            app.canvas.create_line(*[v for p in app.planned.world for v in xy(p)],fill='#29edca',width=3,arrow='last')
        destination=app.world_destination
        if destination and destination['continent']==self.continent:
            x,y=xy(destination['world'])
            app.canvas.create_oval(x-7,y-7,x+7,y+7,outline='white',width=2)
            app.canvas.create_text(x+12,y-10,text='Destination',anchor='w',fill='white')
        packet=app.connection['packet'] if app.connection else None
        if packet and packet.get('world') and packet.get('continent')==self.continent:
            x,y=xy(packet['world']);facing=math.radians(packet.get('facing',0.))
            app.canvas.create_oval(x-6,y-6,x+6,y+6,outline='#ffc76b',width=2)
            app.canvas.create_line(x,y,x+22*math.sin(facing),y-22*math.cos(facing),fill='#ffc76b',width=3,arrow='last')
        # Short labels identify overlapping map rectangles without obscuring the artwork.
        for zone_id in sorted(set(self.owners)):
            z=self.atlas.zones[zone_id];left,right,top,bottom=z['bounds']
            x,y=xy(((top+bottom)/2,(left+right)/2,0.))
            app.canvas.create_text(x,y,text=z['name'],fill='#e1efff',font=('Segoe UI',9,'bold'))
