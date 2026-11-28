"""Optional AI check: which way should each cut-out card be turned to be upright?

One request per scan (all its cards together, as small images) to a small, low-cost Claude model.
Fronts follow the main picture; backs follow their text; blank backs stay as they are.
Without an API key the editor works the same, just without this step.
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Literal

import cv2
import numpy as np
from pydantic import BaseModel, Field

ORIENT_MODEL = os.environ.get("LISTER_ORIENT_MODEL", "claude-haiku-5-5")
TURN_FOR = {"up": 0, "left": 90, "down": 180, "right": 270}  # where the top points → degrees clockwise


class CardTop(BaseModel):
    card: int = Field(description="The card's number as labelled")
    top: Literal["up", "down", "left", "right"] = Field(
        description="Where the top of the card's main picture points (or of its text if it has no picture)")


class Orientation(BaseModel):
    cards: list[CardTop]


PROMPT = """These are trading cards cut out of a scan, labelled Card 1, Card 2, … Each may be a front
(picture side) or a back (text, or blank). Some lie sideways or upside down.

For each card, say where the TOP of the card currently points in the image as shown:
- a front: judge by the main picture (people/animals upright), not by caption text; a photo printed
  sideways on the card means the card itself should be turned so the photo is upright
- a back: judge by the printed text
- a blank back: "up"
Answer for every card."""


def _image_block(img: np.ndarray, long_side: int = 512) -> dict:
    h, w = img.shape[:2]
    scale = min(1.0, long_side / max(h, w))
    small = cv2.resize(img, (round(w * scale), round(h * scale)), interpolation=cv2.INTER_AREA) if scale < 1 else img
    ok, buf = cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, 85])
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                        "data": base64.standard_b64encode(buf.tobytes()).decode()}}


class Orienter:
    """Callable for editor_core.process_folder: images → degrees to turn each clockwise."""

    def __init__(self, client, model: str = ORIENT_MODEL):
        self.client, self.model = client, model
        self.cost_usd = 0.0

    def __call__(self, images: list[np.ndarray]) -> list[int] | None:
        from .costs import cost_of

        content: list[dict] = []
        for n, img in enumerate(images, start=1):
            content += [{"type": "text", "text": f"Card {n}:"}, _image_block(img)]
        content.append({"type": "text", "text": PROMPT})
        response = self.client.messages.parse(
            model=self.model,
            max_tokens=4000,
            messages=[{"role": "user", "content": content}],
            output_format=Orientation,
            output_config={"effort": "low"},
        )
        self.cost_usd += cost_of(getattr(response, "model", None) or self.model, getattr(response, "usage", None)) or 0
        if response.stop_reason != "end_turn" or response.parsed_output is None:
            return None  # declined or cut off: keep the cards as scanned
        turns = [0] * len(images)
        for c in response.parsed_output.cards:
            if 1 <= c.card <= len(images):
                turns[c.card - 1] = TURN_FOR[c.top]
        return turns


# ---------------------------------------------------------------- where the editor keeps its API key
def settings_path() -> Path:
    base = os.environ.get("APPDATA") or os.path.join(Path.home(), ".config")
    return Path(base) / "Lister" / "scan-editor.json"


def load_settings() -> dict:
    try:
        return json.loads(settings_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {}


def save_settings(values: dict) -> None:
    p = settings_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({**load_settings(), **values}, indent=2), encoding="utf-8")


def api_key() -> str | None:
    """The key from the editor's settings, else ANTHROPIC_API_KEY."""
    return load_settings().get("anthropic_api_key") or os.environ.get("ANTHROPIC_API_KEY") or None


def make_orienter() -> Orienter | None:
    key = api_key()
    if not key:
        return None
    import anthropic

    return Orienter(anthropic.Anthropic(api_key=key))
