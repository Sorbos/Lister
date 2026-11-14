"""Decide whether two consecutive scans are the fronts and backs of the same cards.

Two checks, both must pass:
1. Shapes: same number of cards, each in the same spot (within ~10 mm, after allowing for the
   whole sheet shifting) and the same size. Flipping a card in place moves it a few mm at most;
   neighbouring cards are ~60 mm apart.
2. Claude looks at both scans at low resolution and says which is fronts, which is backs, and
   whether they're the same cards. This catches two different front scans laid out alike, and
   backs scanned first.
"""

from __future__ import annotations

import base64
from typing import Literal

import cv2
import numpy as np
from pydantic import BaseModel, Field

from .images import Card

POSITION_TOLERANCE = 0.22  # of the card's short side (~10 mm on a standard card)
SIZE_TOLERANCE = 0.02  # 2% of width/height


def shapes_match(a: list[Card], b: list[Card]) -> tuple[bool, str]:
    if len(a) != len(b):
        return False, f"{len(a)} cards in one scan, {len(b)} in the other"
    if not a:
        return False, "no cards found"
    ca = np.array([c.center for c in a])
    cb = np.array([c.center for c in b])
    # Whole-sheet shift: median offset to each card's nearest counterpart.
    nearest = [cb[np.argmin(np.linalg.norm(cb - p, axis=1))] - p for p in ca]
    shift = np.median(nearest, axis=0)
    used = set()
    for card, p in zip(a, ca):
        d = np.linalg.norm(cb - (p + shift), axis=1)
        j = int(np.argmin(d))
        tol = POSITION_TOLERANCE * min(card.size)
        if d[j] > tol or j in used:
            return False, f"card {card.index} has no counterpart in the same spot"
        used.add(j)
        (wa, ha), (wb, hb) = card.size, b[j].size
        if abs(wa - wb) > SIZE_TOLERANCE * wa or abs(ha - hb) > SIZE_TOLERANCE * ha:
            return False, f"card {card.index} is a different size in the other scan"
    return True, "cards line up"


class SheetCheck(BaseModel):
    first: Literal["fronts", "backs", "unclear"] = Field(description="What scan 1 shows")
    second: Literal["fronts", "backs", "unclear"] = Field(description="What scan 2 shows")
    same_cards: bool = Field(description="True if one scan is the other side of the same cards")
    reason: str = Field(description="One short sentence")


SHEET_PROMPT = """These are two consecutive scans from a trading card seller. They place up to 9 cards
on the scanner, scan, flip each card where it lies, and scan again, so normally one scan shows the
fronts and the next shows the backs of the same cards. Sometimes they only scan fronts, so scan 2
may instead be the fronts of a different set of cards.

For each scan say whether it shows card fronts (the picture side) or backs (stats/text, logos,
checklists, or plain/blank). Then say whether the two scans are the two sides of the same cards
(compare names, numbers, layout and card positions; a blank back can't be compared, judge by
positions and sizes)."""


def _sheet_image(img: np.ndarray, long_side: int = 1200) -> dict:
    h, w = img.shape[:2]
    scale = long_side / max(h, w)
    small = cv2.resize(img, (round(w * scale), round(h * scale)), interpolation=cv2.INTER_AREA) if scale < 1 else img
    ok, buf = cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, 85])
    data = base64.standard_b64encode(buf.tobytes()).decode()
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": data}}


def check_sheets(img_a: np.ndarray, img_b: np.ndarray, *, client, model: str, effort: str = "low"):
    """Ask Claude which scan is fronts/backs. Returns (SheetCheck, model that answered, cost in $)."""
    from .costs import cost_of

    content = [{"type": "text", "text": "Scan 1:"}, _sheet_image(img_a),
               {"type": "text", "text": "Scan 2:"}, _sheet_image(img_b),
               {"type": "text", "text": SHEET_PROMPT}]
    response = client.beta.messages.parse(
        model=model,
        max_tokens=4000,
        messages=[{"role": "user", "content": content}],
        output_format=SheetCheck,
        output_config={"effort": effort},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    )
    served = getattr(response, "model", None) or model
    usd = cost_of(served, getattr(response, "usage", None))
    if response.stop_reason != "end_turn" or response.parsed_output is None:
        return None, served, usd
    return response.parsed_output, served, usd
