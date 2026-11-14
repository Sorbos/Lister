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

## Review and approve

```bash
lister review out/batch-001
```

Opens a page in your browser (served only on your own computer) showing all 9 cards with their
front and back. For each card you can:
- fix any field; fields Claude was unsure of are highlighted ⚠, and the title rebuilds as you type
  (or type your own title; "Auto title" switches back)
- choose **Auction** (start price, 1–10 days, optional Buy It Now and reserve) or **Fixed price**
- mark it "Don't list", or **Approve** it
- set a **start time** (empty = goes live as soon as it's listed); the page shows when each auction ends

**Staggered endings:** under "Stagger start times", pick when the first card starts and the gap
(e.g. every 3 minutes), then "Apply". Cards are scheduled in scanner order, so 9 seven-day auctions
starting Mon 7:00 PM end Mon 7:00, 7:03, … 7:24 PM a week later. eBay allows scheduling up to 3 weeks ahead.

Set batch defaults at the top (e.g. 7-day auctions from $0.99) and apply them to every card.
Problems are listed under each card and block approval (empty title, over 80 characters, no price,
Buy It Now less than 30% above the start price). Everything is saved to `review.json` as you go,
so you can stop and come back. It also works without `lister identify`; you just fill in fields yourself.

## Tests

```bash
pytest
```
