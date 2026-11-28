"""Draw the Scan Editor icon (green backdrop, two cards) and save it as a multi-size .ico."""
import struct
import sys
from pathlib import Path

import cv2
import numpy as np


def draw(size: int = 256) -> np.ndarray:
    s = size * 4  # draw large, then shrink for smooth edges
    img = np.zeros((s, s, 4), np.uint8)
    green = (74, 122, 46, 255)  # BGR + alpha: the scanner-sheet green
    r = s // 6
    cv2.rectangle(img, (r, 0), (s - r, s), green, -1)
    cv2.rectangle(img, (0, r), (s, s - r), green, -1)
    for cx, cy in ((r, r), (s - r, r), (r, s - r), (s - r, s - r)):
        cv2.circle(img, (cx, cy), r, green, -1, lineType=cv2.LINE_AA)

    def card(center, angle, fill, edge):
        w, h = s * 0.42, s * 0.6
        box = cv2.boxPoints(((center[0], center[1]), (w, h), angle)).astype(np.int32)
        cv2.fillConvexPoly(img, box, fill, lineType=cv2.LINE_AA)
        cv2.polylines(img, [box], True, edge, thickness=max(2, s // 90), lineType=cv2.LINE_AA)
        return box

    card((s * 0.40, s * 0.52), -12, (236, 244, 247, 255), (60, 60, 60, 255))
    front = card((s * 0.60, s * 0.48), 8, (250, 252, 252, 255), (60, 60, 60, 255))
    # a little "photo" on the front card
    c = front.mean(axis=0)
    photo = cv2.boxPoints(((c[0], c[1] - s * 0.05), (s * 0.30, s * 0.34), 8)).astype(np.int32)
    cv2.fillConvexPoly(img, photo, (196, 132, 43, 255), lineType=cv2.LINE_AA)
    return cv2.resize(img, (size, size), interpolation=cv2.INTER_AREA)


def write_ico(path: Path, sizes=(16, 24, 32, 48, 64, 128, 256)) -> None:
    big = draw(256)
    frames = []
    for n in sizes:
        im = cv2.resize(big, (n, n), interpolation=cv2.INTER_AREA)
        ok, buf = cv2.imencode(".png", im)
        frames.append((n, buf.tobytes()))
    header = struct.pack("<HHH", 0, 1, len(frames))
    offset = 6 + 16 * len(frames)
    entries, data = b"", b""
    for n, png in frames:
        dim = 0 if n >= 256 else n
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(png), offset + len(data))
        data += png
    path.write_bytes(header + entries + data)


if __name__ == "__main__":
    out = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).with_name("scan-editor.ico"))
    write_ico(out)
    cv2.imwrite(str(out.with_suffix(".png")), draw(256))
    print("wrote", out)
