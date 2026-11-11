"""Build per-zone vector data for the live maps (run once after maps change).
Input : exported id-images + labels from mapviewer AGMaps._export (scratch dir), maps/maps.json, maps/zXXXX.webp
Output: maps/v/<zid>.json  {Y:[r,g,b], p:[[x,y,...flat] | null per plot], l:[[x,y,a,f,w] | null]}
        maps/t/<zid>.webp  map image with paper background + title bar made transparent
"""
import sys, json, os
import cv2, numpy as np
src, repo = sys.argv[1], sys.argv[2]
items = json.load(open(os.path.join(repo, 'maps/maps.json')))['items']
os.makedirs(os.path.join(repo, 'maps/v'), exist_ok=True); os.makedirs(os.path.join(repo, 'maps/t'), exist_ok=True)
tot = 0
for it in items:
    z = it['zid']; lab = json.load(open(f'{src}/{z}.json'))
    idimg = cv2.imread(f'{src}/{z}.png', cv2.IMREAD_UNCHANGED)
    ids = idimg[:, :, 2].astype(np.int32) + (idimg[:, :, 1].astype(np.int32) << 8)  # BGR(A): R=low, G=high
    polys = []
    for k in range(len(it['plots'])):
        m = (ids == k + 1).astype(np.uint8)
        if m.sum() < 20: polys.append(None); continue
        m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))  # close label holes
        cs, _ = cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        c = max(cs, key=cv2.contourArea); c = cv2.approxPolyDP(c, 0.9, True).reshape(-1, 2)
        polys.append([int(v) for xy in c for v in xy]); tot += len(c)
    json.dump({'Y': lab['Y'], 'p': polys, 'l': lab['labs']}, open(os.path.join(repo, f'maps/v/{z}.json'), 'w'), separators=(',', ':'))
    # transparent map image
    im = cv2.imread(os.path.join(repo, it['file']), cv2.IMREAD_COLOR); h, w = im.shape[:2]
    light = im.reshape(-1, 3); light = light[light.sum(1) > 640]
    q = (light >> 2).astype(np.int32); key = q[:, 0] * 4096 + q[:, 1] * 64 + q[:, 2]
    v, cnt = np.unique(key, return_counts=True); kk = v[cnt.argmax()]; paper = np.array([(kk // 4096) * 4 + 2, ((kk // 64) % 64) * 4 + 2, (kk % 64) * 4 + 2])
    d = np.sqrt(((im.astype(np.int32) - paper) ** 2).sum(2))
    a = np.clip((d - 16) / 18, 0, 1) * 255
    a[: int(h * 0.105), :] = 0
    rgba = np.dstack([im, a.astype(np.uint8)])
    cv2.imwrite(os.path.join(repo, f'maps/t/{z}.webp'), rgba, [cv2.IMWRITE_WEBP_QUALITY, 82])
print('zones', len(items), 'points', tot)
