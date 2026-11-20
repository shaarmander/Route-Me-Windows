"""Canvas pen interactions for cubic roads; no game input or navigation queries."""
from collections import deque
from copy import deepcopy
import math

from road_curves import anchor, closest, flatten, from_polyline, move_anchor, move_handle, split


class RoadPenEditor:
    def __init__(self, app):
        self.app=app
        self.history=deque(maxlen=50)
        self.reset()

    def reset(self):
        self.road=self.node=None
        self.draft=None;self.dragging=None;self.before=None
        self.history.clear()

    def ensure_curves(self):
        app=self.app
        app.road_curves=(app.road_curves+[None]*len(app.roads))[:len(app.roads)]

    def snapshot(self):
        self.ensure_curves()
        return deepcopy((self.app.roads,self.app.road_curves))

    def curve(self):
        if self.draft is not None: return self.draft
        if self.road is None or self.road>=len(self.app.roads): return None
        self.ensure_curves()
        if self.app.road_curves[self.road] is None:
            self.app.road_curves[self.road]=from_polyline(self.app.roads[self.road])
        return self.app.road_curves[self.road]

    def dimensions(self):
        h,w=self.app.plan_image.shape[:2]
        return w,h

    def normalized(self,p):
        w,h=self.dimensions()
        return p[0]/w,p[1]/h

    def pixels(self,p):
        w,h=self.dimensions()
        return p[0]*w,p[1]*h

    def save(self,roads,curves,before):
        app=self.app;w,h=self.dimensions()
        try:
            sampled=[flatten(c,w,h) if c is not None else r for r,c in zip(roads,curves)]
            app.persist_roads(sampled,curves)
        except (OSError,ValueError) as error:
            app.roads,app.road_curves=deepcopy(before)
            app.status.set(f'Curve edit could not be saved: {error}')
            app.render();return False
        self.history.append(before)
        app.status.set('Curve saved. Drag anchors or handles; Alt-drag separates handles. Double-click a road to add an anchor.')
        app.render();return True

    def new_road(self):
        if self.app.busy(): return
        if self.draft is not None and len(self.draft)>=2 and not self.finish(): return
        self.road=self.node=None;self.draft=[];self.dragging=None
        self.app.status.set('Pen: click for a corner, click-drag for a curve. Enter or Finish road saves it; Esc cancels.')
        self.app.render()

    def finish(self):
        if self.app.busy(): return False
        if self.draft is None: return True
        if len(self.draft)<2:
            self.app.status.set('Place at least two anchors before finishing the road.')
            return False
        before=self.snapshot()
        curves=deepcopy(self.app.road_curves)+[deepcopy(self.draft)]
        roads=list(self.app.roads)+[[]]
        if not self.save(roads,curves,before): return False
        self.road=len(roads)-1;self.node=len(self.draft)-1
        self.draft=None;self.dragging=None;self.app.render()
        return True

    def cancel(self):
        if self.before is not None:
            self.app.roads,self.app.road_curves=deepcopy(self.before)
        self.draft=None;self.dragging=None;self.before=None;self.node=self.road=None
        self.app.drawing=False
        self.app.status.set('Pen selection cancelled. Saved roads retained.')
        self.app.render()

    def hit_node(self,p):
        curve=self.curve()
        if not curve: return None
        candidates=[]
        for index,node in enumerate(curve):
            for key in ('point','incoming','outgoing'):
                # Show and hit only the selected anchor's tangent controls.
                if key!='point' and (index!=self.node or node[key]==node['point']): continue
                x,y=self.pixels(node[key]);error=math.hypot(x-p[0],y-p[1])*self.app.scale
                if error<=8: candidates.append((error,0 if key=='point' else 1,index,key))
        return min(candidates)[2:] if candidates else None

    def down(self,p):
        self.app.canvas.focus_set()
        hit=self.hit_node(p)
        if hit:
            self.before=self.snapshot();self.node,key=hit;self.dragging=(self.node,key)
        elif not all(0<=v<=1 for v in self.normalized(p)):
            return
        elif self.draft is not None:
            self.draft.append(anchor(self.normalized(p),smooth=True))
            self.node=len(self.draft)-1;self.dragging=(self.node,'new')
        else:
            w,h=self.dimensions();best=None
            for index,road in enumerate(self.app.roads):
                self.ensure_curves()
                curve=self.app.road_curves[index] or from_polyline(road)
                found=closest(curve,p,w,h)
                if found and found[0]*self.app.scale<=8 and (best is None or found[0]<best[0]):
                    best=(found[0],index)
            if best is not None:
                self.road=best[1];self.node=None
                hit=self.hit_node(p)
                if hit:
                    self.before=self.snapshot();self.node,key=hit;self.dragging=(self.node,key)
                self.app.status.set('Road selected. Drag its anchors; select an anchor to show its handles. Double-click the line to add an anchor.')
            else:
                self.new_road()
                self.draft.append(anchor(self.normalized(p),smooth=True));self.node=0;self.dragging=(0,'new')
        self.app.drawing=self.dragging is not None
        self.app.render()

    def drag(self,p,independent=False):
        if self.dragging is None: return
        index,key=self.dragging;node=self.curve()[index]
        point=self.normalized(p)
        if key=='point': move_anchor(node,tuple(max(0.,min(1.,v)) for v in point))
        elif key=='new': move_handle(node,'outgoing',point,independent)
        else: move_handle(node,key,point,independent)
        self.app.render()

    def up(self):
        self.app.drawing=False
        if self.dragging is not None and self.draft is None and self.before is not None:
            self.save(self.app.roads,self.app.road_curves,self.before)
        self.dragging=None;self.before=None
        self.app.render()

    def insert(self,p):
        if self.app.busy(): return
        if self.draft is not None:
            self.finish();return
        curve=self.curve()
        if curve is None: return
        w,h=self.dimensions();found=closest(curve,p,w,h)
        if not found or found[0]*self.app.scale>10: return
        _,index,t=found
        if t<.001 or t>.999: return
        before=self.snapshot()
        self.app.road_curves[self.road]=split(curve,index,t)
        self.node=index+1
        self.save(self.app.roads,self.app.road_curves,before)

    def change_node(self,smooth):
        if self.app.busy() or self.node is None: return
        curve=self.curve()
        if curve is None: return
        before=self.snapshot();node=curve[self.node];p=node['point']
        if not smooth:
            node['incoming']=node['outgoing']=p;node['smooth']=False
        else:
            previous=curve[max(0,self.node-1)]['point'];following=curve[min(len(curve)-1,self.node+1)]['point']
            delta=tuple((following[j]-previous[j])/6 for j in (0,1))
            node['incoming']=tuple(p[j]-delta[j] for j in (0,1));node['outgoing']=tuple(p[j]+delta[j] for j in (0,1))
            node['smooth']=True
        if self.draft is None: self.save(self.app.roads,self.app.road_curves,before)
        else: self.app.render()

    def delete_anchor(self):
        if self.app.busy() or self.node is None: return
        curve=self.curve()
        if curve is None: return
        if self.draft is not None:
            del curve[self.node];self.node=None;self.app.render();return
        if len(curve)<=2:
            self.app.status.set('A road needs two anchors. Use Delete road to remove it.');return
        before=self.snapshot();del curve[self.node];self.node=None
        self.save(self.app.roads,self.app.road_curves,before)

    def delete_road(self):
        if self.app.busy() or self.road is None or self.draft is not None: return
        before=self.snapshot();index=self.road
        roads=self.app.roads[:index]+self.app.roads[index+1:]
        curves=self.app.road_curves[:index]+self.app.road_curves[index+1:]
        self.road=self.node=None
        self.save(roads,curves,before)

    def undo(self):
        if self.app.busy(): return
        if self.draft is not None:
            if self.draft: self.draft.pop()
            self.node=None;self.dragging=None;self.app.drawing=False
            self.app.status.set('Last pen anchor removed. Enter finishes the road.');self.app.render();return
        if not self.history: return
        roads,curves=self.history[-1]
        try: self.app.persist_roads(deepcopy(roads),deepcopy(curves))
        except (OSError,ValueError) as error:
            self.app.status.set(f'Could not undo edit: {error}');return
        self.history.pop();self.road=self.node=None;self.draft=None
        self.app.status.set('Last curve edit undone and saved.');self.app.render()

    def render(self,xy):
        if self.app.mode!='pen' or self.app.view!='plan': return
        curve=self.curve()
        if curve is None: return
        canvas=self.app.canvas;w,h=self.dimensions()
        if len(curve)>1:
            coords=[v for p in flatten(curve,w,h) for v in xy(self.pixels(p))]
            canvas.create_line(*coords,fill='#b6dbff',width=4)
        for index,node in enumerate(curve):
            x,y=xy(self.pixels(node['point']))
            if index==self.node:
                for key in ('incoming','outgoing'):
                    hx,hy=xy(self.pixels(node[key]))
                    canvas.create_line(x,y,hx,hy,fill='#ffe094',width=1)
                    canvas.create_oval(hx-4,hy-4,hx+4,hy+4,fill='#ffe094',outline='#19293b',width=1)
            canvas.create_rectangle(x-4,y-4,x+4,y+4,fill='#ffffff' if index==self.node else '#73b9ff',outline='#17212b',width=1)
