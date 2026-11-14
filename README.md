# Lister

Turn trading-card scans (9 cards at a time) into individual, straightened card photos, and, soon,
into eBay listings. See [docs/PLAN.md](docs/PLAN.md) for the roadmap.

## Install

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

## Split a scan

Scan the 9 fronts, flip each card where it lies, scan the backs. Then:

```bash
lister split fronts.jpg backs.jpg -o out/batch-001
```

`out/batch-001` gets `card_01_front.jpg`, `card_01_back.jpg`, … `card_09_back.jpg` and a
`manifest.json`. Cards are numbered row by row, left to right, as they lay on the scanner.

Options:
- `-n 0` to accept any number of cards (default expects 9).
- `--margin 0.05` for more border around each card.
- `--layout mirror` if you flip a whole row like a page instead of each card in place.
- Backs are optional: `lister split fronts.jpg -o out/x`.

Tips: use a solid backdrop that contrasts with the card borders (the green sheet works well),
and leave a finger-width gap between cards.

## Identify the cards (Claude)

Needs an Anthropic API key in `ANTHROPIC_API_KEY` (put `export ANTHROPIC_API_KEY=...` in a local
`.env` you source; it is git-ignored).

```bash
lister identify out/batch-001
```

Each card's front and back go to Claude, which returns year, set, player/subject, team, card #,
rookie/auto/patch/serial, a condition estimate, and which fields it is unsure of. Results land in
`out/batch-001/cards.json` with a ready-made eBay title, e.g.

    2005-06 Upper Deck Young Guns Sidney Crosby Pittsburgh Penguins #201 RC

Cards scanned upside down are rotated automatically. Options: `--model`, `--effort low|medium|high`
(defaults: `claude-opus-5-5`, `medium`; also settable via `LISTER_MODEL` / `LISTER_EFFORT`).
If Claude ever declines a card, the request automatically falls back to another model.

## Tests

```bash
pytest
```
