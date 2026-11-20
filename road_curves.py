"""Editable normalized cubic Bezier roads; flattening retains curve geometry."""
from copy import deepcopy
import math
from core import distance


def anchor(point, smooth=False):
    p=tuple(point)
    return dict(point=p,incoming=p,outgoing=p,smooth=smooth)


def from_polyline(points):
    return [anchor(p) for p in points]


def validate_curve(curve, point_bounds=(0.,1.), handle_bounds=(-4.,5.)):
    if not isinstance(curve,list) or not 2<=len(curve)<=20000:
        raise ValueError('A road curve needs at least two anchors.')
    result=[]
    for node in curve:
        if not isinstance(node,dict) or type(node.get('smooth')) is not bool:
            raise ValueError('Invalid road anchor.')
        found={}
        for key in ('point','incoming','outgoing'):
            p=node.get(key)
            lo,hi=point_bounds if key=='point' else handle_bounds
            if (not isinstance(p,(tuple,list)) or len(p)!=2 or any(isinstance(v,bool) or
                    not isinstance(v,(float,int)) or not math.isfinite(v) or not lo<=v<=hi for v in p)):
                raise ValueError('Road anchors must be inside the map with finite handles.')
            found[key]=tuple(map(float,p))
        found['smooth']=node['smooth'];result.append(found)
    return result


def mix(a,b,t=.5):
    return tuple(a[j]+(b[j]-a[j])*t for j in (0,1))


def cubic(a,b,t):
    p0,p1,p2,p3=a['point'],a['outgoing'],b['incoming'],b['point']
    return tuple((1-t)**3*p0[j]+3*(1-t)**2*t*p1[j]+3*(1-t)*t*t*p2[j]+t**3*p3[j] for j in (0,1))


def flatten(curve,width,height,tolerance=.2,with_parameters=False):
    """Adaptive world-independent sampling within a fraction of a map pixel."""
    if not curve: return []
    result=[(curve[0]['point'],0,0.)]
    def deviation(p,a,b):
        px,py=p[0]*width,p[1]*height;ax,ay=a[0]*width,a[1]*height;bx,by=b[0]*width,b[1]*height
        dx,dy=bx-ax,by-ay;length2=dx*dx+dy*dy
        t=max(0.,min(1.,((px-ax)*dx+(py-ay)*dy)/length2)) if length2 else 0.
        return math.hypot(px-ax-t*dx,py-ay-t*dy)
    def subdivide(p0,p1,p2,p3,index,lo,hi,depth):
        if depth>=16 or max(deviation(p1,p0,p3),deviation(p2,p0,p3))<=tolerance:
            result.append((p3,index,hi))
            if len(result)>20000: raise ValueError('Road curve is too complex; use fewer anchors or shorter handles.')
            return
        q0,q1,q2=mix(p0,p1),mix(p1,p2),mix(p2,p3)
        r0,r1=mix(q0,q1),mix(q1,q2);s=mix(r0,r1);mid=(lo+hi)/2
        subdivide(p0,q0,r0,s,index,lo,mid,depth+1)
        subdivide(s,r1,q2,p3,index,mid,hi,depth+1)
    for index,(a,b) in enumerate(zip(curve,curve[1:])):
        subdivide(a['point'],a['outgoing'],b['incoming'],b['point'],index,0.,1.,0)
    return result if with_parameters else [p for p,_,_ in result]


def move_anchor(node, point):
    delta=tuple(point[j]-node['point'][j] for j in (0,1))
    for key in ('point','incoming','outgoing'):
        node[key]=tuple(node[key][j]+delta[j] for j in (0,1))


def move_handle(node,key,point,independent=False):
    node[key]=tuple(point)
    if independent: node['smooth']=False
    if node['smooth']:
        other='incoming' if key=='outgoing' else 'outgoing'
        node[other]=tuple(2*node['point'][j]-point[j] for j in (0,1))


def split(curve,index,t):
    """Insert an anchor using de Casteljau, without changing the road shape."""
    result=deepcopy(curve);a,b=result[index:index+2]
    q0=mix(a['point'],a['outgoing'],t);q1=mix(a['outgoing'],b['incoming'],t);q2=mix(b['incoming'],b['point'],t)
    r0,r1=mix(q0,q1,t),mix(q1,q2,t);p=mix(r0,r1,t)
    a['outgoing']=q0;b['incoming']=q2
    result.insert(index+1,dict(point=p,incoming=r0,outgoing=r1,smooth=True))
    return result


def closest(curve,point,width,height):
    samples=flatten(curve,width,height,tolerance=.1,with_parameters=True)
    best=None
    for (a,ai,at),(b,bi,bt) in zip(samples,samples[1:]):
        ax,ay=a[0]*width,a[1]*height;dx,dy=(b[0]-a[0])*width,(b[1]-a[1])*height
        length2=dx*dx+dy*dy
        s=max(0.,min(1.,((point[0]-ax)*dx+(point[1]-ay)*dy)/length2)) if length2 else 0.
        error=math.hypot(point[0]-ax-s*dx,point[1]-ay-s*dy)
        t=(at if ai==bi else 0.)*(1-s)+bt*s
        if best is None or error<best[0]: best=(error,bi,t,at if ai==bi else 0.,bt)
    if best is None: return None
    _,index,t,lo,hi=best
    def error_at(t):
        p=cubic(curve[index],curve[index+1],t)
        return math.hypot(p[0]*width-point[0],p[1]*height-point[1])
    # Flat cubic segments can have nonlinear parameter speed. Refine on the
    # actual cubic so an inserted anchor appears under the user's cursor.
    for _ in range(32):
        a=lo+(hi-lo)/3;b=hi-(hi-lo)/3
        if error_at(a)<error_at(b): hi=b
        else: lo=a
    t=(lo+hi)/2
    return error_at(t),index,t
