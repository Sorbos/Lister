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


def _background_mask(img: np.ndarray) -> np.ndarray:
    """Return a uint8 mask, 255 where the pixel is NOT background."""
    lab = cv2.cvtColor(cv2.GaussianBlur(img, (5, 5), 0), cv2.COLOR_BGR2LAB).astype(np.float32)
    h, w = lab.shape[:2]
    b = max(5, min(h, w) // 100)
    border = np.concatenate(
        [lab[:b].reshape(-1, 3), lab[-b:].reshape(-1, 3), lab[:, :b].reshape(-1, 3), lab[:, -b:].reshape(-1, 3)]
    )
    bg = np.median(border, axis=0)
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


def load(path: str | Path) -> np.ndarray:
    img = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if img is None:
        raise FileNotFoundError(path)
    return img


def save(img: np.ndarray, path: str | Path, quality: int = 95) -> None:
    cv2.imwrite(str(path), img, [cv2.IMWRITE_JPEG_QUALITY, quality])


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
    (out / "manifest.json").write_text(json.dumps({"source": source, "cards": manifest}, indent=2))
    return manifest


def rotate_file(path: str | Path, degrees_clockwise: int) -> None:
    """Turn an image file in place by a multiple of 90°."""
    turns = {90: cv2.ROTATE_90_CLOCKWISE, 180: cv2.ROTATE_180, 270: cv2.ROTATE_90_COUNTERCLOCKWISE}
    rot = turns.get(int(degrees_clockwise) % 360)
    if rot is not None:
        save(cv2.rotate(load(path), rot), path)
