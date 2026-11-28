"""Split a multi-card scan into individual, deskewed card images.

The approach does not assume a fixed grid:
1. Estimate the background colour from the scan's outer border.
2. Mark every pixel that differs enough from it as "card".
3. Take each large blob, fit a rotated rectangle to it, and warp that
   rectangle (plus a small margin so all four corners are visible) upright.
4. Order the cards in reading order (row by row, left to right).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np


@dataclass
class Card:
    """One card found in a scan."""

    index: int  # 1-based reading-order position in the scan
    center: tuple[float, float]  # (x, y) in scan pixels
    size: tuple[float, float]  # (width, height) of the card itself, in pixels
    angle: float  # degrees the card was rotated on the scanner glass
    corners: np.ndarray = field(repr=False)  # 4x2 float32, TL, TR, BR, BL
    image: np.ndarray = field(repr=False)  # cropped, upright BGR image


class DetectionError(RuntimeError):
    pass


COLOURED_BACKDROP = 10  # backdrop chroma (Lab a/b distance from grey) above which we judge by its colour


def _background_mask(img: np.ndarray) -> np.ndarray:
    """Return a uint8 mask, 255 where the pixel is NOT background.

    With a coloured backdrop (like a green sheet) a pixel is background only if it has the backdrop's
    colour: grey, black and white areas of a card never count, which matters for black-and-white
    photos printed to the card's edge. With a grey/black/white backdrop, it falls back to "how
    different from the backdrop is this pixel", with an automatic threshold.
    """
    lab = cv2.cvtColor(cv2.GaussianBlur(img, (7, 7), 0), cv2.COLOR_BGR2LAB).astype(np.float32)
    h, w = lab.shape[:2]
    b = max(5, min(h, w) // 100)
    border = np.concatenate(
        [lab[:b].reshape(-1, 3), lab[-b:].reshape(-1, 3), lab[:, :b].reshape(-1, 3), lab[:, -b:].reshape(-1, 3)]
    )
    bg = np.median(border, axis=0)
    bg_ab = bg[1:] - 128
    chroma = float(np.linalg.norm(bg_ab))
    if chroma >= COLOURED_BACKDROP:
        d_ab = np.linalg.norm(lab[..., 1:] - 128 - bg_ab, axis=2)
        d_l = np.abs(lab[..., 0] - bg[0])
        is_bg = (d_ab < max(8.0, 0.45 * chroma)) & (d_l < 45)
        mask = np.where(is_bg, 0, 255).astype(np.uint8)
    else:
        dist = np.linalg.norm(lab - bg, axis=2)
        dist8 = np.clip(dist, 0, 255).astype(np.uint8)
        _, mask = cv2.threshold(dist8, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    k = max(3, min(h, w) // 300) | 1
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (k, k))
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)  # drop specks / dust
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=3)  # fill gaps
    # Fill holes so dark artwork inside a card can't split it.
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    filled = np.zeros_like(mask)
    cv2.drawContours(filled, contours, -1, 255, thickness=cv2.FILLED)
    return filled


def _order_corners(pts: np.ndarray) -> np.ndarray:
    """Order 4 points as top-left, top-right, bottom-right, bottom-left."""
    pts = pts.astype(np.float32)
    s = pts.sum(axis=1)
    d = np.diff(pts, axis=1).ravel()
    return np.array([pts[np.argmin(s)], pts[np.argmin(d)], pts[np.argmax(s)], pts[np.argmax(d)]], dtype=np.float32)


def _warp(img: np.ndarray, corners: np.ndarray, margin_frac: float) -> np.ndarray:
    tl, tr, br, bl = corners
    w = (np.linalg.norm(tr - tl) + np.linalg.norm(br - bl)) / 2
    h = (np.linalg.norm(bl - tl) + np.linalg.norm(br - tr)) / 2
    m = round(max(w, h) * margin_frac)
    W, H = round(w) + 2 * m, round(h) + 2 * m
    dst = np.array([[m, m], [m + w, m], [m + w, m + h], [m, m + h]], dtype=np.float32)
    M = cv2.getPerspectiveTransform(corners, dst)
    return cv2.warpPerspective(img, M, (W, H), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)


def _reading_order(centers: list[tuple[float, float]], heights: list[float]) -> list[int]:
    """Group cards into rows (by y), then sort each row left to right."""
    order = sorted(range(len(centers)), key=lambda i: centers[i][1])
    rows: list[list[int]] = []
    for i in order:
        if rows and abs(centers[i][1] - np.mean([centers[j][1] for j in rows[-1]])) < 0.5 * heights[i]:
            rows[-1].append(i)
        else:
            rows.append([i])
    return [i for row in rows for i in sorted(row, key=lambda j: centers[j][0])]


def detect_cards(
    img: np.ndarray,
    expected: int | None = 9,
    margin_frac: float = 0.03,
    min_area_frac: float = 0.005,
) -> list[Card]:
    """Find cards in a BGR scan and return them upright in reading order.

    expected: raise DetectionError if a different number is found (None to skip).
    margin_frac: extra border around each card, as a fraction of its long side.
    """
    mask = _background_mask(img)
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    min_area = min_area_frac * img.shape[0] * img.shape[1]

    found = []
    for c in contours:
        if cv2.contourArea(c) < min_area:
            continue
        (cx, cy), (rw, rh), ang = cv2.minAreaRect(c)
        # Fill ratio rejects odd shapes (e.g. two touching cards).
        if cv2.contourArea(c) / (rw * rh) < 0.85:
            continue
        corners = _order_corners(cv2.boxPoints(((cx, cy), (rw, rh), ang)))
        tl, tr, br, bl = corners
        width = float(np.linalg.norm(tr - tl))
        height = float(np.linalg.norm(bl - tl))
        tilt = float(np.degrees(np.arctan2(tr[1] - tl[1], tr[0] - tl[0])))
        found.append(((cx, cy), (width, height), tilt, corners))

    # Ignore blobs much smaller than the typical card (stickers, debris).
    if found:
        med = np.median([w * h for _, (w, h), _, _ in found])
        found = [f for f in found if f[1][0] * f[1][1] > 0.5 * med]

    if expected is not None and len(found) != expected:
        raise DetectionError(f"Expected {expected} cards but found {len(found)}")

    order = _reading_order([f[0] for f in found], [f[1][1] for f in found])
    return [
        Card(
            index=n,
            center=found[i][0],
            size=found[i][1],
            angle=found[i][2],
            corners=found[i][3],
            image=_warp(img, found[i][3], margin_frac),
        )
        for n, i in enumerate(order, start=1)
    ]


def pair_backs(fronts: list[Card], backs: list[Card], layout: str = "same") -> list[tuple[Card, Card]]:
    """Match each front with its back by position on the scanner.

    layout="same": each card was flipped where it lay (back is in the same spot).
    layout="mirror": rows were flipped like a page, so left/right are swapped.
    Matching is by nearest centre, so small shifts between scans are fine.
    """
    if len(fronts) != len(backs):
        raise DetectionError(f"{len(fronts)} fronts but {len(backs)} backs")
    xs = [c.center[0] for c in fronts + backs]
    mid_x = (min(xs) + max(xs)) / 2

    def target(c: Card) -> np.ndarray:
        x, y = c.center
        return np.array([2 * mid_x - x if layout == "mirror" else x, y])

    remaining = list(backs)
    pairs = []
    for f in fronts:
        t = target(f)
        b = min(remaining, key=lambda b: float(np.linalg.norm(np.array(b.center) - t)))
        if np.linalg.norm(np.array(b.center) - t) > 0.5 * min(f.size):
            raise DetectionError(
                f"Card {f.index}: no back near its position. Were the cards moved between scans, "
                f"or flipped like a page? (try layout='{'same' if layout == 'mirror' else 'mirror'}')"
            )
        remaining.remove(b)
        pairs.append((f, b))
    return pairs


# OpenCV's imread/imwrite can't open Windows paths with non-English letters (e.g. C:\\Users\\Dániel),
# so files are read and written by Python and only decoded/encoded by OpenCV.
def load(path: str | Path) -> np.ndarray:
    try:
        data = np.fromfile(str(path), dtype=np.uint8)
    except OSError:
        raise FileNotFoundError(path) from None
    img = cv2.imdecode(data, cv2.IMREAD_COLOR) if data.size else None
    if img is None:
        raise FileNotFoundError(f"{path} (missing, empty or not an image)")
    return img


def save(img: np.ndarray, path: str | Path, quality: int = 95) -> None:
    ext = Path(path).suffix.lower() or ".jpg"
    params = [cv2.IMWRITE_JPEG_QUALITY, quality] if ext in (".jpg", ".jpeg") else []
    ok, buf = cv2.imencode(ext, img, params)
    if not ok:
        raise OSError(f"Couldn't encode image for {path}")
    buf.tofile(str(path))


def write_batch(fronts: list[Card], backs: list[Card] | None, out: str | Path, source: dict,
                layout: str = "same") -> list[dict]:
    """Save each card's front (and back) image plus manifest.json into OUT. Returns the manifest cards."""
    import json

    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    pairs = pair_backs(fronts, backs, layout) if backs else [(f, None) for f in fronts]
    manifest = []
    for f, b in pairs:
        stem = f"card_{f.index:02d}"
        save(f.image, out / f"{stem}_front.jpg")
        entry = {"position": f.index, "front": f"{stem}_front.jpg", "tilt_deg": round(f.angle, 2)}
        if b is not None:
            save(b.image, out / f"{stem}_back.jpg")
            entry["back"] = f"{stem}_back.jpg"
        manifest.append(entry)
    (out / "manifest.json").write_text(json.dumps({"source": source, "cards": manifest}, indent=2), encoding="utf-8")
    return manifest


def rotate_file(path: str | Path, degrees_clockwise: int) -> None:
    """Turn an image file in place by a multiple of 90°."""
    turns = {90: cv2.ROTATE_90_CLOCKWISE, 180: cv2.ROTATE_180, 270: cv2.ROTATE_90_COUNTERCLOCKWISE}
    rot = turns.get(int(degrees_clockwise) % 360)
    if rot is not None:
        save(cv2.rotate(load(path), rot), path)


def _edge_map(img: np.ndarray) -> np.ndarray:
    gray = cv2.GaussianBlur(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), (5, 5), 0).astype(np.float32)
    gx = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
    return cv2.magnitude(gx, gy)


def _perimeter_score(edges: np.ndarray, center, size, angle: float) -> float:
    """How strongly the image has edges along this rectangle's outline."""
    pts = cv2.boxPoints((tuple(center), tuple(size), angle))
    samples = []
    for k in range(4):
        a, b = pts[k], pts[(k + 1) % 4]
        t = np.linspace(0.08, 0.92, 40)[:, None]  # skip the corners themselves
        samples.append(a + (b - a) * t)
    xy = np.concatenate(samples).round().astype(int)
    h, w = edges.shape
    ok = (xy[:, 0] >= 0) & (xy[:, 0] < w) & (xy[:, 1] >= 0) & (xy[:, 1] < h)
    if ok.mean() < 0.9:
        return 0.0
    return float(edges[xy[ok, 1], xy[ok, 0]].mean())


def _refine_rect(edges: np.ndarray, center, size, angle: float, reach: float) -> tuple:
    """Nudge a rectangle (position ±reach px, angle ±4°) to sit on the card's real edges."""
    best = (_perimeter_score(edges, center, size, angle), tuple(center), angle)
    for step, span, astep, aspan in ((reach / 8, reach, 1.0, 4.0), (2.0, reach / 8, 0.25, 1.0)):
        cx, cy = best[1]
        a0 = best[2]
        for dx in np.arange(-span, span + 0.1, step):
            for dy in np.arange(-span, span + 0.1, step):
                for da in np.arange(-aspan, aspan + 0.01, astep):
                    sc = _perimeter_score(edges, (cx + dx, cy + dy), size, a0 + da)
                    if sc > best[0]:
                        best = (sc, (cx + dx, cy + dy), a0 + da)
    return best[1], best[2]


def match_cards(mine: list[Card], partner: list[Card]) -> tuple[set[int], np.ndarray]:
    """Pair cards of two scans of the same sheet one-to-one, closest pairs first.

    Returns (indexes of partner cards that have a counterpart in `mine`, the whole-sheet shift from
    partner to mine). Cards can move a few mm when flipped; neighbouring cards are several cm apart,
    and a counterpart must also be about the same size.
    """
    if not mine or not partner:
        return set(), np.zeros(2)
    a = np.array([c.center for c in mine])
    b = np.array([c.center for c in partner])
    shift = np.median([a[np.argmin(np.linalg.norm(a - p, axis=1))] - p for p in b], axis=0)
    if np.linalg.norm(shift) > 0.5 * min(partner[0].size):
        shift = np.zeros(2)  # sheets don't line up: don't trust it
    targets = b + shift
    limit = 0.6 * min(min(c.size) for c in partner)
    pairs = sorted((float(np.linalg.norm(m - t)), i, j) for i, m in enumerate(a) for j, t in enumerate(targets))
    used, matched = set(), set()
    for dist, i, j in pairs:
        if dist > limit:
            break
        if i in used or j in matched:
            continue
        (wa, ha), (wb, hb) = sorted(mine[i].size), sorted(partner[j].size)
        if abs(wa - wb) > 0.08 * wb or abs(ha - hb) > 0.08 * hb:
            continue  # different card shape here
        used.add(i)
        matched.add(j)
    return matched, shift


def recover_from_partner(img: np.ndarray, cards: list[Card], partner: list[Card],
                         margin_frac: float = 0.03) -> tuple[list[Card], int]:
    """Cut out cards this scan missed, using where they are in its partner scan (front ↔ back).

    Cards are flipped where they lie, so a card found in one scan is in the same spot in the other
    (allowing for the whole sheet shifting a little). A spot is only used if something that isn't
    backdrop is actually there. Returns (cards in reading order, number recovered).
    """
    if len(partner) <= len(cards):
        return cards, 0
    matched, shift = match_cards(cards, partner)
    targets = np.array([c.center for c in partner]) + shift
    mask = _background_mask(img)
    found = [(c.center, c.size, c.angle, c.corners) for c in cards]
    added = 0
    for j, p in enumerate(partner):
        if j in matched:
            continue  # this scan has it already
        target = targets[j]
        corners = (p.corners + shift).astype(np.float32)
        region = np.zeros(mask.shape, np.uint8)
        cv2.fillConvexPoly(region, corners.astype(np.int32), 255)
        inside = cv2.countNonZero(region)
        if not inside or cv2.countNonZero(cv2.bitwise_and(mask, region)) / inside < 0.2:
            continue  # nothing but backdrop there
        # The card may have moved a little when flipped: fit the outline to its real edges.
        if "edges" not in locals():
            edges = _edge_map(img)
        (w_, h_) = p.size
        center, angle = _refine_rect(edges, target, (w_, h_), p.angle, reach=0.2 * min(p.size))
        corners = _order_corners(cv2.boxPoints((center, (w_, h_), angle)))
        found.append((tuple(center), p.size, angle, corners))
        added += 1
    if not added:
        return cards, 0
    order = _reading_order([f[0] for f in found], [f[1][1] for f in found])
    out = [Card(index=n, center=found[i][0], size=found[i][1], angle=found[i][2], corners=found[i][3],
                image=_warp(img, found[i][3], margin_frac))
           for n, i in enumerate(order, start=1)]
    return out, added
