"""Map display transform only; zoom never changes stored route coordinates."""
class Viewport:
    def __init__(self):
        self.zoom = 1.
        self.pan = (0.,0.)
        self.size = (1,1,1,1)
        self.scale = 1.
        self.offset = (0.,0.)

    def layout(self, cw, ch, w, h):
        self.size = cw,ch,w,h
        self.scale = min(cw/w,ch/h)*self.zoom
        limits = (max(0,(w*self.scale-cw)/2),max(0,(h*self.scale-ch)/2))
        self.pan = tuple(max(-limit,min(limit,value)) for value,limit in zip(self.pan,limits))
        self.offset = ((cw-w*self.scale)/2+self.pan[0],(ch-h*self.scale)/2+self.pan[1])

    def point(self, x, y):
        return ((x-self.offset[0])/self.scale,(y-self.offset[1])/self.scale)

    def zoom_at(self, factor, x, y):
        point = self.point(x,y)
        self.zoom = max(1.,min(8.,self.zoom*factor))
        cw,ch,w,h = self.size
        scale = min(cw/w,ch/h)*self.zoom
        self.pan = (x-point[0]*scale-(cw-w*scale)/2,y-point[1]*scale-(ch-h*scale)/2)
        self.layout(*self.size)

    def reset(self):
        self.zoom, self.pan = 1.,(0.,0.)
        self.layout(*self.size)
