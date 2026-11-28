"""Command-line entry point: `lister split FRONTS [BACKS] -o OUTDIR`."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .images import DetectionError, detect_cards, load, pair_backs, write_batch


def cmd_split(args: argparse.Namespace) -> int:
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    expected = args.count or None
    try:
        fronts = detect_cards(load(args.fronts), expected=expected, margin_frac=args.margin)
        backs = detect_cards(load(args.backs), expected=len(fronts), margin_frac=args.margin) if args.backs else []
        if backs:
            pair_backs(fronts, backs, args.layout)  # check pairing before writing anything
    except DetectionError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    manifest = write_batch(fronts, backs or None, out,
                           {"fronts": str(args.fronts), "backs": str(args.backs) if args.backs else None}, args.layout)
    print(f"Wrote {len(manifest)} cards to {out}")
    return 0


def cmd_identify(args: argparse.Namespace) -> int:
    import os

    from .config import load_dotenv
    from .watcher import explain_error

    load_dotenv()  # .env in the current folder…
    folder = Path(args.folder).resolve()
    if not args.folder or not (folder / "manifest.json").exists():
        print(f"error: {args.folder or '(no folder given)'} isn't a batch folder (no manifest.json). "
              "Give the folder `lister split` wrote, or a folder under <Lister>/batches.", file=sys.stderr)
        return 1
    # …or the nearest .env above the batch (e.g. C:\\Lister\\.env for C:\\Lister\\batches\\<batch>)
    for parent in list(folder.parents)[:4]:
        if (parent / ".env").is_file():
            load_dotenv(parent / ".env")
            break
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        print(f"error: no ANTHROPIC_API_KEY. Put it in a .env file in {folder.parent} (or a folder above it)",
              file=sys.stderr)
        return 1
    from .identify import DEFAULT_EFFORT, DEFAULT_MODEL, identify_batch

    results = identify_batch(folder, model=args.model or os.environ.get("LISTER_MODEL") or DEFAULT_MODEL,
                             effort=args.effort or DEFAULT_EFFORT)
    errors = [r["error"] for r in results if r["error"]]
    print(f"Identified {len(results) - len(errors)}/{len(results)} cards → {folder / 'cards.json'}")
    if errors:
        print(f"Why: {explain_error(errors[0])}", file=sys.stderr)
    return 1 if errors else 0


def cmd_review(args: argparse.Namespace) -> int:
    from .review import ReviewServer

    ReviewServer(args.folder, port=args.port).serve(open_browser=not args.no_browser)
    return 0


def default_root() -> Path:
    import os

    if os.environ.get("LISTER_ROOT"):
        return Path(os.environ["LISTER_ROOT"])
    return Path(r"C:\Lister") if os.name == "nt" else Path.home() / "Lister"


def cmd_watch(args: argparse.Namespace) -> int:
    import threading

    from .config import load_dotenv
    from .review import ReviewServer
    from .watcher import Watcher

    root = Path(args.root) if args.root else default_root()
    root.mkdir(parents=True, exist_ok=True)
    load_dotenv(root / ".env")  # keys can live in C:\Lister\.env
    load_dotenv()
    import os

    client = None
    if not args.no_read:
        if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
            print(f"No Anthropic API key found, so cards will be cut out and paired but not read.\n"
                  f"To have Claude read them, put ANTHROPIC_API_KEY=... in {root / '.env'} and restart.")
        else:
            import anthropic

            client = anthropic.Anthropic()
    watcher = Watcher(root, client=client, auto_identify=client is not None, pair_timeout=args.wait * 60 if args.wait is not None else None)
    server = ReviewServer(root, port=args.port, hub=True)
    threading.Thread(target=server.serve, kwargs={"open_browser": not args.no_browser}, daemon=True).start()
    print(f"Scan into {watcher.inbox} (change it on the Settings page). New batches show up on the review page.")
    try:
        watcher.run()
    except KeyboardInterrupt:
        print("\nStopped watching. Scans still in the inbox will be picked up next time.")
    return 0


def cmd_batches(args: argparse.Namespace) -> int:
    from .review import ReviewServer

    root = Path(args.root) if args.root else default_root()
    ReviewServer(root, port=args.port, hub=True).serve()
    return 0


def cmd_usage(args: argparse.Namespace) -> int:
    from .costs import ledger_path, money, summary

    s = summary()
    if not s["cards"]:
        print("No Claude calls recorded yet.")
        return 0
    print(f"Claude cost (estimated from list prices; your Anthropic Console has the exact bill)")
    print(f"  Last 7 days:  {money(s['last_7_days'])}")
    print(f"  This month:   {money(s['this_month'])}")
    print(f"  All time:     {money(s['all_time'])}  ({s['cards']} cards, {money(s['all_time'] / s['cards'])} per card)")
    if len(s["by_model"]) > 1:
        print("  By model:")
        for m, usd in sorted(s["by_model"].items(), key=lambda kv: -kv[1]):
            print(f"    {m:<20} {money(usd)}")
    recent = list(s["by_folder"].items())[-5:]
    if recent:
        print("  Recent batches:")
        for f, usd in reversed(recent):
            print(f"    {f:<20} {money(usd)}")
    if s["unpriced"]:
        print(f"  ({s['unpriced']} card(s) used a model with no known price and aren't counted)")
    print(f"Log: {ledger_path()}")
    return 0


def _ebay():
    from .config import EbaySettings
    from .ebay_api import EbayClient

    settings = EbaySettings.load()
    return settings, EbayClient(settings)


def _pick(label: str, options: list[tuple[str, str]], current: str | None) -> str:
    """Numbered menu; returns the chosen id."""
    if not options:
        raise SystemExit(f"No {label} found on your eBay account. Create one in Seller Hub, then run this again.")
    print(f"\n{label}:")
    for i, (oid, name) in enumerate(options, 1):
        print(f"  {i}. {name}{'  (current)' if oid == current else ''}")
    while True:
        raw = input(f"Choose 1-{len(options)}" + (" [Enter keeps current]" if current else "") + ": ").strip()
        if not raw and current:
            return current
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return options[int(raw) - 1][0]


def cmd_ebay_login(args: argparse.Namespace) -> int:
    import webbrowser

    settings, client = _ebay()
    url = client.consent_url()
    print(f"Signing in to eBay ({settings.environment}).")
    print("1. A browser window opens (or open this link yourself):\n   " + url)
    print("2. Sign in and click 'Agree'.")
    print("3. Copy the full address of the page you land on and paste it here.")
    webbrowser.open(url)
    client.sign_in(input("\nAddress: "))
    print("Signed in. Next: lister ebay setup")
    return 0


def cmd_ebay_setup(args: argparse.Namespace) -> int:
    settings, client = _ebay()
    for kind in ("fulfillment", "payment", "return"):
        opts = [(p[f"{kind}PolicyId"], p.get("name", "")) for p in client.policies(kind)]
        label = {"fulfillment": "Shipping policy", "payment": "Payment policy", "return": "Return policy"}[kind]
        setattr(settings, f"{kind}_policy_id", _pick(label, opts, getattr(settings, f"{kind}_policy_id")))
    locs = [(l["merchantLocationKey"], l.get("name") or l["merchantLocationKey"]) for l in client.locations()]
    if locs:
        settings.location_key = _pick("Item location", locs, settings.location_key)
    else:
        print("\nYou have no item location yet; eBay needs one (only city/postcode is shown to buyers).")
        postal = input("Postal/ZIP code the cards ship from: ").strip()
        country = input("Country code (US, CA, GB, AU): ").strip().upper()
        client.create_location("lister-home", "Lister", postal, country)
        settings.location_key = "lister-home"
    print("\nScheduled start times:")
    print("  1. eBay starts them. Works with your computer off; eBay may charge a small fee per scheduled listing.")
    print("  2. Lister starts them. Free, but `lister list` must stay running until the last card goes live.")
    choice = input(f"Choose 1-2 [current: {'1' if settings.schedule_mode == 'ebay' else '2'}]: ").strip()
    if choice in ("1", "2"):
        settings.schedule_mode = "ebay" if choice == "1" else "local"
    settings.save()
    print(f"\nSaved to {settings.saved_path}")
    return 0


def cmd_ebay_status(args: argparse.Namespace) -> int:
    from .ebay_api import NotSignedIn

    settings, client = _ebay()
    print(f"Environment: {settings.environment}   Marketplace: {settings.marketplace} ({settings.currency})")
    try:
        client.access_token()
        print("Signed in: yes")
    except NotSignedIn as e:
        print(f"Signed in: no ({e})")
    for k in ("fulfillment_policy_id", "payment_policy_id", "return_policy_id", "location_key", "schedule_mode"):
        print(f"{k}: {getattr(settings, k) or '(not set: run lister ebay setup)'}")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    from .config import EbaySettings
    from .ebay_api import EbayError, NotSignedIn
    from .listing import ListingError, approved_cards, list_batch

    if args.dry_run and not EbaySettings.has_keys():
        settings, client = EbaySettings.offline(), None
        print("(No eBay keys yet: previewing offline with placeholder policies.)")
    else:
        settings, client = _ebay()
    if args.dry_run:
        if client is not None:
            try:
                client.access_token()
            except NotSignedIn:
                print("(Not signed in: previewing without eBay's category details.)")
                client = None
    else:
        n = len(approved_cards(Path(args.folder)))
        where = "the eBay SANDBOX (test site)" if settings.environment == "sandbox" else "eBay — LIVE listings"
        if not args.yes and input(f"List {n} approved card(s) on {where}? Type yes: ").strip().lower() != "yes":
            print("Cancelled.")
            return 1
    try:
        list_batch(args.folder, settings, client, dry_run=args.dry_run)
    except (ListingError, EbayError, NotSignedIn) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nStopped. Progress is saved; run the same command again to continue.")
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):  # Windows consoles may not show …, ¢, —
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
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

    w = sub.add_parser("watch", help="Process scans that land in the inbox folder automatically")
    w.add_argument("--root", default=None,
                   help=r"Lister folder (default: C:\Lister on Windows, ~/Lister elsewhere, or $LISTER_ROOT)")
    w.add_argument("--port", type=int, default=8765)
    w.add_argument("--no-browser", action="store_true")
    w.add_argument("--no-read", action="store_true", help="Cut out and pair cards, but don't read them with Claude")
    w.add_argument("--wait", type=float, default=None,
                   help="Minutes a lone scan waits for its back scan (default: Settings page, 10)")
    w.set_defaults(func=cmd_watch)

    rv = sub.add_parser("batches", help="Open the page listing every batch (without watching the inbox)")
    rv.add_argument("--root", default=None)
    rv.add_argument("--port", type=int, default=8765)
    rv.set_defaults(func=cmd_batches)

    u = sub.add_parser("usage", help="How much Lister's Claude calls have cost, in dollars")
    u.set_defaults(func=cmd_usage)

    e = sub.add_parser("ebay", help="Connect Lister to your eBay account")
    esub = e.add_subparsers(dest="ebay_cmd", required=True)
    esub.add_parser("login", help="Sign in to eBay (once; stays signed in for about 18 months)").set_defaults(func=cmd_ebay_login)
    esub.add_parser("setup", help="Pick shipping/payment/return policies and item location").set_defaults(func=cmd_ebay_setup)
    esub.add_parser("status", help="Show eBay connection and setup").set_defaults(func=cmd_ebay_status)

    l = sub.add_parser("list", help="List the approved cards in a folder on eBay")
    l.add_argument("folder")
    l.add_argument("--dry-run", action="store_true", help="Show exactly what would be sent; send nothing")
    l.add_argument("--yes", action="store_true", help="Don't ask for confirmation")
    l.set_defaults(func=cmd_list)
    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
