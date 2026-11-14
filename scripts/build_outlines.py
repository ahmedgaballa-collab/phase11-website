"""Zone outlines for the satellite view: the red dashed zone border on each map sheet, closed and filled.
Run: cd /tmp && python3 -I <repo>/scripts/build_outlines.py <outdir> [zid ...]  -> <outdir>/outlines.json (+ preview jpgs);
then keep outlines covering <= 62% of the sheet and save them as maps/outlines.json (larger ones are the sheet frame, not a border)."""
import sys, os, json, cv2, numpy as np
repo=os.path.dirname(os.path.dirname(os.path.abspath(__file__))); out=sys.argv[1]; zs=sys.argv[2:]
items={x['zid']:x for x in json.load(open(os.path.join(repo,'maps/maps.json')))['items']}
res={}
for z in (zs or items):
    it=items[z]; im=cv2.imread(os.path.join(repo,it['file'])); h,w=im.shape[:2]
    b,g,r=[im[...,i].astype(np.int32) for i in range(3)]
    red=((r>140)&(r-g>80)&(r-b>70)).astype(np.uint8)
    red=cv2.morphologyEx(red,cv2.MORPH_OPEN,np.ones((3,3),np.uint8))
    # remove red that is inside plots (plot numbers) using vectors
    V=json.load(open(os.path.join(repo,f'maps/v/{z}.json')))
    inside=np.zeros((h,w),np.uint8)
    for pg in V['p']:
        if pg: cv2.fillPoly(inside,[np.array(pg,np.int32).reshape(-1,2)],1)
    red[cv2.dilate(inside,np.ones((9,9),np.uint8))>0]=0
    fr=red.copy(); fr[:, :6]=1; fr[:, -6:]=1; fr[int(h*.105)-3:int(h*.105)+3]=1; fr[int(h*.962)-3:int(h*.962)+3]=1  # sheet frame closes borders that run off the map
    P=np.array([[p[2],p[3]] for p in it['plots']],np.float32)
    best=None
    for ks in (81,121,161,221,381,421):
      if best: break
      k=cv2.getStructuringElement(cv2.MORPH_ELLIPSE,(ks if ks<300 else ks-300,)*2)
      cl=cv2.morphologyEx(red if ks<300 else fr,cv2.MORPH_CLOSE,k)
      cs,_=cv2.findContours(cl,cv2.RETR_LIST,cv2.CHAIN_APPROX_SIMPLE)
      for c in cs:
        A=cv2.contourArea(c)
        if A<2000 or A>0.9*w*h: continue
        n=sum(cv2.pointPolygonTest(c,(float(x),float(y)),False)>=0 for x,y in P)
        if n>=max(1,(0.9 if ks<300 else 0.85)*len(P)) and (best is None or A<best[0]): best=(A,c,n)
    if best is None: res[z]=None; print(z,'NONE',len(P)); continue
    c=cv2.approxPolyDP(best[1],2,True).reshape(-1,2)
    res[z]=[int(v) for xy in c for v in xy]
    print(z,'ok area%',round(best[0]/(w*h)*100,1),'pts',len(c),'plots in',best[2],'/',len(P))
    vis=im.copy(); cv2.polylines(vis,[c.astype(np.int32)],True,(255,0,255),6)
    cv2.imwrite(os.path.join(out,f'{z}.jpg'),vis[::3,::3])
json.dump(res,open(os.path.join(out,'outlines.json'),'w'))
