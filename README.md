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

## Tests

```bash
pytest
```
