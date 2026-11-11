"""Map sheets for the satellite view: paper + title bar transparent, and the red dashed zone border removed
(on the satellite it looked like booked plots lying on the streets).
Output: maps/t/<zid>.webp (full) and maps/t/s/<zid>.webp (900px wide, used when zoomed out).
Run: cd /tmp && python3 -I <repo>/scripts/build_tiles.py <repo>"""
import sys, os, json, cv2, numpy as np
repo = sys.argv[1]
items = json.load(open(os.path.join(repo, 'maps/maps.json')))['items']
for it in items:
    z = it['zid']
    im = cv2.imread(os.path.join(repo, it['file']), cv2.IMREAD_COLOR); h, w = im.shape[:2]
    light = im.reshape(-1, 3); light = light[light.sum(1) > 640]
    q = (light >> 2).astype(np.int32); key = q[:, 0] * 4096 + q[:, 1] * 64 + q[:, 2]
    v, cnt = np.unique(key, return_counts=True); kk = v[cnt.argmax()]; paper = np.array([(kk // 4096) * 4 + 2, ((kk // 64) % 64) * 4 + 2, (kk % 64) * 4 + 2])
    d = np.sqrt(((im.astype(np.int32) - paper) ** 2).sum(2))
    a = np.clip((d - 16) / 18, 0, 1) * 255
    a[: int(h * 0.105), :] = 0
    b, g, r = [im[..., i].astype(np.int32) for i in range(3)]
    red = ((r > 140) & (r - g > 80) & (r - b > 70)).astype(np.uint8)
    red = cv2.morphologyEx(red, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))  # dashes are solid blocks; thin red text stays
    red = cv2.dilate(red, np.ones((5, 5), np.uint8))
    inside = np.zeros((h, w), np.uint8)  # keep red inside plots (their numbers)
    for pg in json.load(open(os.path.join(repo, f'maps/v/{z}.json')))['p']:
        if pg: cv2.fillPoly(inside, [np.array(pg, np.int32).reshape(-1, 2)], 1)
    red[cv2.dilate(inside, np.ones((7, 7), np.uint8)) > 0] = 0
    a[red > 0] = 0
    rgba = np.dstack([im, a.astype(np.uint8)])
    cv2.imwrite(os.path.join(repo, f'maps/t/{z}.webp'), rgba, [cv2.IMWRITE_WEBP_QUALITY, 82])
    sw = 900; sh = round(h * sw / w)
    cv2.imwrite(os.path.join(repo, f'maps/t/s/{z}.webp'), cv2.resize(rgba, (sw, sh), interpolation=cv2.INTER_AREA), [cv2.IMWRITE_WEBP_QUALITY, 80])
print('done', len(items))
