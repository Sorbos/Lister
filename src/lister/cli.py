"""Command-line entry point: `lister split FRONTS [BACKS] -o OUTDIR`."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .images import DetectionError, detect_cards, load, pair_backs, save


def cmd_split(args: argparse.Namespace) -> int:
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    expected = args.count or None
    try:
        fronts = detect_cards(load(args.fronts), expected=expected, margin_frac=args.margin)
        backs = detect_cards(load(args.backs), expected=len(fronts), margin_frac=args.margin) if args.backs else []
        pairs = pair_backs(fronts, backs, args.layout) if backs else [(f, None) for f in fronts]
    except DetectionError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    manifest = []
    for f, b in pairs:
        stem = f"card_{f.index:02d}"
        save(f.image, out / f"{stem}_front.jpg")
        entry = {"position": f.index, "front": f"{stem}_front.jpg", "tilt_deg": round(f.angle, 2)}
        if b is not None:
            save(b.image, out / f"{stem}_back.jpg")
            entry["back"] = f"{stem}_back.jpg"
        manifest.append(entry)
    (out / "manifest.json").write_text(json.dumps({"source": {"fronts": str(args.fronts), "backs": str(args.backs) if args.backs else None}, "cards": manifest}, indent=2))
    print(f"Wrote {len(manifest)} cards to {out}")
    return 0


def cmd_identify(args: argparse.Namespace) -> int:
    from .identify import DEFAULT_EFFORT, DEFAULT_MODEL, identify_batch

    results = identify_batch(args.folder, model=args.model or DEFAULT_MODEL, effort=args.effort or DEFAULT_EFFORT)
    failed = sum(1 for r in results if r["error"])
    print(f"Identified {len(results) - failed}/{len(results)} cards → {Path(args.folder) / 'cards.json'}")
    return 1 if failed else 0


def cmd_review(args: argparse.Namespace) -> int:
    from .review import ReviewServer

    ReviewServer(args.folder, port=args.port).serve(open_browser=not args.no_browser)
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="lister")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("split", help="Split a multi-card scan into individual card images")
    s.add_argument("fronts", help="Scan of the card fronts")
    s.add_argument("backs", nargs="?", help="Scan of the card backs (same cards, flipped)")
    s.add_argument("-o", "--out", required=True, help="Output folder")
    s.add_argument("-n", "--count", type=int, default=9, help="Cards per scan (0 = any). Default 9")
    s.add_argument("--margin", type=float, default=0.03, help="Border around each card, fraction of long side")
    s.add_argument("--layout", choices=["same", "mirror"], default="same",
                   help="same: each card flipped in place (default). mirror: rows flipped like a page")
    s.set_defaults(func=cmd_split)

    i = sub.add_parser("identify", help="Read each card in a split folder with Claude; writes cards.json")
    i.add_argument("folder", help="Output folder from `lister split`")
    i.add_argument("--model", default=None, help="Claude model (default claude-opus-5-5, or $LISTER_MODEL)")
    i.add_argument("--effort", default=None, choices=["low", "medium", "high", "xhigh", "max"],
                   help="Reasoning effort (default medium, or $LISTER_EFFORT)")
    i.set_defaults(func=cmd_identify)

    r = sub.add_parser("review", help="Open the review page for a split/identified folder")
    r.add_argument("folder", help="Output folder from `lister split` (and optionally `lister identify`)")
    r.add_argument("--port", type=int, default=8765)
    r.add_argument("--no-browser", action="store_true", help="Don't open the browser automatically")
    r.set_defaults(func=cmd_review)
    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
