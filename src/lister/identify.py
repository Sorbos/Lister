"""Phase 2: read each card's front/back with Claude and return structured details."""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Literal, Optional

import anthropic
from pydantic import BaseModel, Field

DEFAULT_MODEL = os.environ.get("LISTER_MODEL", "claude-opus-5-5")
DEFAULT_EFFORT = os.environ.get("LISTER_EFFORT", "medium")

Condition = Literal["near_mint_or_better", "excellent", "very_good", "poor"]


class CardInfo(BaseModel):
    """What Claude reports about one card. Unknown values are null, never guessed."""

    category: Literal["sports", "non_sport"]
    sport: Optional[str] = Field(description="e.g. Ice Hockey, Baseball. Null for non-sport cards.")
    league: Optional[str] = Field(description="e.g. NHL, WHA, OHL. Null if not shown/known.")
    year: Optional[str] = Field(description="As collectors list it: a season like '1990-91' for hockey, else '1978'.")
    manufacturer: Optional[str] = Field(description="e.g. O-Pee-Chee, Upper Deck, Topps")
    set_name: Optional[str] = Field(description="Product/series name WITHOUT the year, e.g. 'Upper Deck', 'SP Authentic'")
    subset_insert: Optional[str] = Field(description="Insert or subset name, e.g. 'Young Guns', 'All-Star'")
    parallel: Optional[str] = Field(description="Parallel/variation, e.g. 'Gold', 'Refractor'. Null for base cards.")
    card_number: Optional[str] = Field(description="Number printed on the card, without '#'")
    subject: str = Field(description="Player name(s), or the character/subject for non-sport cards")
    team: Optional[str]
    rookie: bool = Field(description="True only if marked as a rookie card (RC logo, 'Rookie', Young Guns, etc.)")
    autograph: bool
    memorabilia: bool = Field(description="Contains a jersey/patch/stick swatch")
    serial_number: Optional[str] = Field(description="Serial numbering exactly as printed, e.g. '23/99'")
    graded: bool = Field(description="True if the card is in a grading slab")
    grader: Optional[str]
    grade: Optional[str]
    condition: Condition = Field(description="Visible condition estimate for an ungraded card")
    front_orientation: Literal["up", "down", "left", "right"] = Field(
        description="Where the top of the MAIN PICTURE on the FRONT points in the image as shown (ignore caption "
                    "text): up = already upright, left = picture's top points left, etc."
    )
    back_orientation: Optional[Literal["up", "down", "left", "right"]] = Field(
        description="Where the top of the BACK's text points in the image as shown. Null if no back or it's blank."
    )
    confidence: Literal["high", "medium", "low"]
    uncertain_fields: list[str] = Field(description="Names of fields you are not sure about")
    notes: str = Field(description="Anything a seller should double-check. Empty string if nothing.")


SYSTEM = """You identify trading cards for an eBay seller.

You get the front and (when available) the back of ONE card. Read the printed text first; use
your knowledge of card sets only to fill gaps the card itself doesn't answer, and when you do,
list that field in uncertain_fields.

Rules:
- Never invent a value. If something is not printed and you don't know it, use null.
- year: hockey and other winter sports use seasons ("1990-91"). Use the card's product year,
  not the copyright year, when they differ and you can tell.
- card_number: exactly as printed (e.g. "60", "201", "YG-12"), without a '#'.
- rookie: only when the card is a recognised rookie card.
- condition: judge corners, edges, surface and centering from the images; be conservative.
- A blank back is normal for some vintage and non-sport cards; say so in notes only if relevant.
- front_orientation follows the main PICTURE, not the text: some cards print the photo sideways or
  upside down relative to the caption, and the seller wants the picture upright (a sideways photo
  makes the card landscape). back_orientation follows the back's text."""


def _image_block(path: Path) -> dict:
    media = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
    data = base64.standard_b64encode(path.read_bytes()).decode()
    return {"type": "image", "source": {"type": "base64", "media_type": media, "data": data}}


class IdentifyError(RuntimeError):
    def __init__(self, msg: str, usd: float | None = None, model: str | None = None):
        super().__init__(msg)
        self.usd, self.model = usd, model  # a declined/unusable answer is still billed


def identify_card(
    front: Path,
    back: Path | None = None,
    *,
    client: anthropic.Anthropic | None = None,
    model: str = DEFAULT_MODEL,
    effort: str = DEFAULT_EFFORT,
) -> tuple[CardInfo, str, float | None]:
    """Returns (card info, model that answered, cost in dollars or None if unknown)."""
    from .costs import cost_of

    client = client or anthropic.Anthropic()
    content: list[dict] = [{"type": "text", "text": "FRONT:"}, _image_block(Path(front))]
    if back is not None:
        content += [{"type": "text", "text": "BACK:"}, _image_block(Path(back))]
    content.append({"type": "text", "text": "Identify this card."})

    response = client.beta.messages.parse(
        model=model,
        max_tokens=16000,
        system=SYSTEM,
        messages=[{"role": "user", "content": content}],
        output_format=CardInfo,
        output_config={"effort": effort},
        # If the model declines (rare, false-positive safety refusals), the API
        # retries on a suitable fallback model inside the same call.
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    )
    served = getattr(response, "model", None) or model  # may differ if a fallback model answered
    usd = cost_of(served, getattr(response, "usage", None))
    if response.stop_reason == "refusal":
        raise IdentifyError(f"Model declined to identify {front}", usd, served)
    if response.stop_reason == "max_tokens" or response.parsed_output is None:
        raise IdentifyError(f"No usable answer for {front} (stop_reason={response.stop_reason})", usd, served)
    return response.parsed_output, served, usd


def identify_batch(
    folder: str | Path,
    *,
    client: anthropic.Anthropic | None = None,
    model: str = DEFAULT_MODEL,
    effort: str = DEFAULT_EFFORT,
    progress=print,
    on_card=None,
) -> list[dict]:
    """Identify every card in a `lister split` output folder; writes cards.json there."""
    from .costs import money, record
    from .title import build_title

    folder = Path(folder)
    manifest = json.loads((folder / "manifest.json").read_text())
    client = client or anthropic.Anthropic()
    results = []
    total, unpriced = 0.0, 0

    def tally(served, usd, position):
        nonlocal total, unpriced
        record(served, usd, folder.name, position)
        if usd is None:
            unpriced += 1
        else:
            total += usd

    n_cards = len(manifest["cards"])
    for entry in manifest["cards"]:
        if on_card and results:
            on_card(len(results), n_cards)
        front = folder / entry["front"]
        back = folder / entry["back"] if entry.get("back") else None
        progress(f"Card {entry['position']}: reading…")
        try:
            info, served, usd = identify_card(front, back, client=client, model=model, effort=effort)
        except (IdentifyError, anthropic.APIStatusError) as e:
            usd = getattr(e, "usd", None)
            if isinstance(e, IdentifyError):
                tally(e.model or model, usd, entry["position"])
            progress(f"Card {entry['position']}: {e}")
            results.append({**entry, "info": None, "title": None, "error": str(e), "cost_usd": usd})
            continue
        tally(served, usd, entry["position"])
        _fix_orientation(front, info.front_orientation)
        if back is not None and info.back_orientation:
            _fix_orientation(back, info.back_orientation)
        title = build_title(info)
        progress(f"Card {entry['position']}: {title}  [{info.confidence}, {money(usd)}]")
        results.append({**entry, "info": info.model_dump(), "title": title, "error": None, "cost_usd": usd})

    if on_card:
        on_card(len(results), len(results))
    (folder / "cards.json").write_text(json.dumps(
        {"model": model, "cost_usd": round(total, 4), "cards": results}, indent=2))
    n = len(results)
    line = f"Claude cost for this batch: {money(total)} ({money(total / n if n else 0)} per card)"
    if unpriced:
        line += f"; {unpriced} card(s) used a model with no known price, not included"
    progress(line)
    progress("Running total: lister usage")
    return results


# Where the top points → degrees to turn clockwise to make it upright.
TURN_FOR = {"up": 0, "left": 90, "down": 180, "right": 270}


def _fix_orientation(path: Path, orientation: str) -> None:
    from .images import rotate_file

    rotate_file(path, TURN_FOR.get(orientation, 0))
