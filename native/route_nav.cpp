// Small C interface to the local, matching Detour implementation. Positions are
// Detour (world Y, world Z, world X). This library never accesses the game.
#include <DetourNavMesh.h>
#include <DetourNavMeshQuery.h>
#include <DetourAlloc.h>
#include <cstring>
#include <cmath>
#include <memory>
#define API extern "C" __declspec(dllexport)
struct Nav {
    dtNavMesh mesh;
    dtNavMeshQuery query;
    dtQueryFilter filter;
    Nav() { filter.setIncludeFlags(1); filter.setExcludeFlags(0xfffe); }
};
static bool complete(dtStatus s) {
    return dtStatusSucceed(s) && !(s & (DT_PARTIAL_RESULT|DT_BUFFER_TOO_SMALL|DT_OUT_OF_NODES));
}
API void* nav_create(const unsigned char* data, int size) {
    if (size != sizeof(dtNavMeshParams)) return nullptr;
    dtNavMeshParams params;
    std::memcpy(&params,data,size);
    if (params.maxTiles<1 || params.maxTiles>8192 ||
        !std::isfinite(params.tileWidth) || !std::isfinite(params.tileHeight) ||
        params.tileWidth<1 || params.tileHeight<1) return nullptr;
    auto n=std::make_unique<Nav>();
    if (!complete(n->mesh.init(&params)) || !complete(n->query.init(&n->mesh,65535))) return nullptr;
    return n.release();
}
API void nav_destroy(Nav* n) { delete n; }
API int nav_add(Nav* n, const unsigned char* data, int size) {
    if (size<sizeof(dtMeshHeader)) return 0;
    dtMeshHeader h; std::memcpy(&h,data,sizeof(h));
    if (h.magic!=DT_NAVMESH_MAGIC || h.version!=DT_NAVMESH_VERSION) return 0;
    int counts[]={h.vertCount,h.polyCount,h.maxLinkCount,h.detailMeshCount,
                  h.detailVertCount,h.detailTriCount,h.bvNodeCount,h.offMeshConCount};
    size_t strides[]={12,sizeof(dtPoly),sizeof(dtLink),sizeof(dtPolyDetail),12,4,
                      sizeof(dtBVNode),sizeof(dtOffMeshConnection)};
    size_t expected=sizeof(dtMeshHeader);
    for (int i=0;i<8;++i) {
        if (counts[i]<0 || counts[i]>1000000) return 0;
        expected+=counts[i]*strides[i];
    }
    if (expected!=size) return 0; // also distinguishes 32-bit and 64-bit tiles
    auto copy=static_cast<unsigned char*>(dtAlloc(size,DT_ALLOC_PERM));
    if (!copy) return 0;
    std::memcpy(copy,data,size);
    auto status=n->mesh.addTile(copy,size,DT_TILE_FREE_DATA,0,nullptr);
    if (!complete(status)) { dtFree(copy); return 0; }
    return 1;
}
API int nav_nearest(Nav* n,const float* p,const float* extent,float* result) {
    dtPolyRef ref=0;
    return complete(n->query.findNearestPoly(p,extent,&n->filter,&ref,result)) && ref!=0;
}
// UnitPosition supplies horizontal location but no usable altitude on this
// client. Enumerate all ground beneath that XY; never pick a floor by fake Z=0.
API int nav_surfaces(Nav* n,const float* p,float* result,int capacity) {
    float center[3]={p[0],0.f,p[2]},extent[3]={.35f,100000.f,.35f};
    dtPolyRef refs[512]; int count=0;
    if (!complete(n->query.queryPolygons(center,extent,&n->filter,refs,&count,512))) return -1;
    float points[512][3]; bool over[512]; bool anyOver=false;
    for (int i=0;i<count;++i) {
        if (!complete(n->query.closestPointOnPoly(refs[i],center,points[i],&over[i]))) return -1;
        anyOver=anyOver || over[i];
    }
    int found=0;
    for (int i=0;i<count;++i) {
        if (anyOver && !over[i]) continue;
        float dx=points[i][0]-p[0],dz=points[i][2]-p[2];
        if (dx*dx+dz*dz>.35f*.35f) continue;
        if (found>=capacity) return -1;
        std::memcpy(result+found*3,points[i],sizeof(points[i]));
        ++found;
    }
    return found;
}
API int nav_inset(Nav* n,const float* p,float margin,float* result) {
    float extent[3]={.15f,2.f,.15f}; dtPolyRef ref=0;
    if (!complete(n->query.findNearestPoly(p,extent,&n->filter,&ref,result)) || !ref) return 0;
    for (int i=0;i<12;++i) {
        float d,hit[3],normal[3];
        if (!complete(n->query.findDistanceToWall(ref,result,margin+.1f,&n->filter,&d,hit,normal))) return 0;
        if (d>=margin-.01f) return 1;
        if (!std::isfinite(normal[0]) || !std::isfinite(normal[2]) || normal[0]*normal[0]+normal[2]*normal[2]<.1f) {
            const dtMeshTile* tile; const dtPoly* poly;
            if (!complete(n->mesh.getTileAndPolyByRef(ref,&tile,&poly))) return 0;
            normal[0]=normal[2]=0;
            for (int k=0;k<poly->vertCount;++k) {
                normal[0]+=tile->verts[poly->verts[k]*3]/poly->vertCount-result[0]/poly->vertCount;
                normal[2]+=tile->verts[poly->verts[k]*3+2]/poly->vertCount-result[2]/poly->vertCount;
            }
            float length=std::sqrt(normal[0]*normal[0]+normal[2]*normal[2]);
            if (length<.00001f) return 0;
            normal[0]/=length; normal[2]/=length;
        }
        float goal[3]={result[0]+normal[0]*(margin-d+.02f),result[1],result[2]+normal[2]*(margin-d+.02f)};
        dtPolyRef visited[64]; int count=0;
        if (!complete(n->query.moveAlongSurface(ref,result,goal,&n->filter,result,visited,&count,64)) || !count) return 0;
        ref=visited[count-1];
        float snapped[3];
        if (!complete(n->query.closestPointOnPoly(ref,result,snapped,nullptr))) return 0;
        std::memcpy(result,snapped,sizeof(snapped));
    }
    return 0;
}
API float nav_clearance(Nav* n,const float* p) {
    float extent[3]={.1f,1.f,.1f},a[3],d,hit[3],normal[3];dtPolyRef ref=0;
    if (!complete(n->query.findNearestPoly(p,extent,&n->filter,&ref,a)) || !ref) return -1;
    if (!complete(n->query.findDistanceToWall(ref,a,2.f,&n->filter,&d,hit,normal))) return -1;
    return d;
}
API int nav_path(Nav* n,const float* start,const float* end,float* result,int capacity) {
    float extent[3]={.15f,2.f,.15f},a[3],b[3];
    dtPolyRef ar=0,br=0,refs[4096];
    if (!complete(n->query.findNearestPoly(start,extent,&n->filter,&ar,a)) || !ar ||
        !complete(n->query.findNearestPoly(end,extent,&n->filter,&br,b)) || !br) return -1;
    int count=0,points=0;
    auto s=n->query.findPath(ar,br,a,b,&n->filter,refs,&count,4096);
    if (!complete(s) || !count || refs[count-1]!=br) return -2;
    s=n->query.findStraightPath(a,b,refs,count,result,nullptr,nullptr,&points,capacity);
    return complete(s)?points:-3;
}
// Connected polygons meeting at one point, constrained to the same surface.
static int groundFan(Nav* n,dtPolyRef origin,const float* point,dtPolyRef* fan,int capacity) {
    fan[0]=origin; int size=1;
    for (int i=0;i<size;++i) {
        const dtMeshTile* tile; const dtPoly* poly;
        if (!complete(n->mesh.getTileAndPolyByRef(fan[i],&tile,&poly))) continue;
        for (unsigned int link=poly->firstLink;link!=DT_NULL_LINK;link=tile->links[link].next) {
            dtPolyRef ref=tile->links[link].ref;
            bool seen=false;
            for (int k=0;k<size;++k) if (fan[k]==ref) { seen=true; break; }
            if (seen || !ref) continue;
            const dtMeshTile* nextTile; const dtPoly* nextPoly;
            if (!complete(n->mesh.getTileAndPolyByRef(ref,&nextTile,&nextPoly)) ||
                nextPoly->getType()!=DT_POLYTYPE_GROUND ||
                !n->filter.passFilter(ref,nextTile,nextPoly)) continue;
            float closest[3];
            if (!complete(n->query.closestPointOnPoly(ref,point,closest,nullptr))) continue;
            float dx=closest[0]-point[0],dz=closest[2]-point[2];
            if (dx*dx+dz*dz>.000004f || std::abs(closest[1]-point[1])>.1f) continue;
            if (size<capacity) fan[size++]=ref;
        }
    }
    return size;
}
API int nav_trace(Nav* n,const float* start,const float* end,float* result) {
    float extent[3]={.12f,1.5f,.12f},a[3],normal[3],t=0;
    dtPolyRef ar=0,refs[4096]; int count=0;
    if (!complete(n->query.findNearestPoly(start,extent,&n->filter,&ar,a)) || !ar) return 0;
    auto s=n->query.raycast(ar,a,end,&n->filter,&t,normal,refs,&count,4096);
    // Float rounding can put a shared-edge start just outside the selected
    // polygon. Retry from its incident ground polygons at the same location.
    if (complete(s) && !count) {
        dtPolyRef fan[64]; int size=groundFan(n,ar,a,fan,64);
        for (int i=1;i<size;++i) {
            s=n->query.raycast(fan[i],a,end,&n->filter,&t,normal,refs,&count,4096);
            if (complete(s) && t>=1.f && count) break;
        }
    }
    if (!complete(s) || t<1.f || !count) return 0;
    float height=0;
    if (!complete(n->query.getPolyHeight(refs[count-1],end,&height))) {
        float closest[3];
        if (!complete(n->query.closestPointOnPoly(refs[count-1],end,closest,nullptr))) return 0;
        float dx=closest[0]-end[0],dz=closest[2]-end[2];
        if (dx*dx+dz*dz>.000004f) return 0;
        height=closest[1];
    }
    // Raycast is horizontal: explicitly reject a destination on another floor.
    if (std::abs(height-end[1])>1.5f) return 0;
    dtPolyRef endRef=0;
    float endNearest[3];
    if (!complete(n->query.findNearestPoly(end,extent,&n->filter,&endRef,endNearest)) || !endRef) return 0;
    // At a shared vertex nearest-poly can choose a polygon several links around
    // the vertex from the raycast result. Walk only the connected ground fan
    // containing this exact endpoint at the traced height, never a remote path
    // or an overlapping floor that merely happens to be nearby.
    if (endRef!=refs[count-1]) {
        float endpoint[3]={end[0],height,end[2]};
        dtPolyRef fan[64]; int size=groundFan(n,refs[count-1],endpoint,fan,64);
        bool connected=false;
        for (int i=0;i<size;++i) if (fan[i]==endRef) { connected=true; break; }
        if (!connected) return 0;
    }
    result[0]=end[0]; result[1]=height; result[2]=end[2];
    return 1;
}
