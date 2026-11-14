# Lister plan

Turn 9-up trading-card scans into eBay listings, from your own computer.

```
fronts.jpg + backs.jpg
  → split: find each card, straighten, crop with corners visible     (Phase 1 ✅)
  → pair each front with its back by position                         (Phase 1 ✅)
  → identify: Claude reads year, set, player/subject, team, card #, RC (Phase 2 ✅)
  → review: local browser page, edit fields/price/format, approve     (Phase 3 ✅)
  → list: upload photos + create listings via eBay Sell APIs          (Phase 4 ✅ built; needs sandbox test)
```

## Decisions so far

| Topic | Decision |
|---|---|
| Card backs | Scanned as a second 9-up scan; each card is flipped where it lies (`--layout same`). `--layout mirror` covers rows flipped like a page. |
| Identification | AI drafts every field, you review before anything is listed. |
| Where it runs | Your computer: Python CLI + local web page in your browser. |
| Listing formats | Both **auction** and **fixed price (Buy It Now / store)**, chosen per card, with a batch default. |
| Title | `Year Set Player Team #CardNo RC [Parallel/Insert]`, max 80 chars, fields dropped from the right if too long. |
| Scans | 300 DPI letter-size JPEG on a solid background (green in the samples). |

## Phase 1: image pipeline ✅
`src/lister/images.py`, `lister split`.
- Background colour is estimated from the scan border, so any solid backdrop works.
- Each card is fitted with a rotated rectangle and warped upright with a 3% margin.
- Cards are ordered row by row, left to right; backs are matched by nearest position,
  with an error if a back isn't where expected.
- Still to do: detect a card placed upside down (Phase 2 will report orientation and we rotate 180°),
  and support landscape cards (works today, they just stay landscape).

## Phase 2: identification (Claude API) ✅
`src/lister/identify.py`, `src/lister/title.py`, `lister identify`.
- Send front + back of each card; get structured JSON back:
  `year, manufacturer, set, subset/insert, parallel, card_number, player_or_subject, team, league, sport,
   rookie, autograph, memorabilia, serial_number ("23/99"), graded, orientation_ok, confidence, notes`.
- Low-confidence fields are flagged on the review page.
- Sport vs non-sport is detected too (your sample is a 1978 Muppets set), since eBay uses a different category.
- Optional later: send all 9 cards of a scan together so the model can use set context.

## Phase 3: review page ✅
`src/lister/review.py`, `src/lister/static/review.html`, `lister review`. Standard library only.
- `lister review OUTDIR` opens a local page: 9 cards (front/back thumbnails, click to zoom).
- Editable fields + live title preview with the 80-char counter.
- Per card: format (Auction / Fixed price), price, auction duration, quantity, condition, skip.
- Batch defaults (e.g. "all auctions, 7 days, start $0.99") applied to all, overridable per card.
- Drafts are saved to the folder so you can stop and come back.

## Phase 4: eBay listing ✅ (built, tested against a simulated eBay; not yet against the real sandbox)
`src/lister/ebay_api.py`, `src/lister/listing.py`, `src/lister/config.py`; `lister ebay login|setup|status`, `lister list`.

To verify on the first sandbox run (from docs/search, not yet exercised for real):
- Media API `getImage` response field `imageUrl`, and the multipart field name `image`.
- Item-specific names for 261328 / 183050 (`Player/Athlete`, `Season`, `Features`, …): Lister reads
  eBay's list at run time, but our name candidates may need adjusting.
- Condition descriptor value names ("Near mint or better", grader names) match our lookups.
- Sandbox accounts may need business policies created via the Account API (no Seller Hub UI).
- Scheduled-listing fee for your marketplace.

Uses eBay's Sell Inventory API (`inventory_item` → `offer` → `publish`) and Media API for photos.
- **Fixed price**: `format=FIXED_PRICE`, Good 'Til Cancelled.
- **Auction**: `format=AUCTION`, start price, optional reserve and Buy It Now price, 1/3/5/7/10 days.
- **Scheduled start**: the review page's per-card `start_time` is sent as the offer's scheduled
  start date, so staggered starts give staggered auction endings. Check eBay's current rules/fees
  for scheduled listings when building this.
- Category: Sports Trading Card Singles (hockey etc.) or Non-Sport Trading Card Singles, picked from the Phase 2 result.
- Item specifics: Sport, League, Player/Athlete, Team, Manufacturer, Set, Season/Year, Card Number,
  Rookie, Parallel/Variety, Autographed, Graded.
- Condition: Ungraded + card-condition descriptor (e.g. Near Mint or Better / Excellent / Very Good / Poor),
  or Graded + grader/grade.
- Shipping/payment/returns: your existing Seller Hub business policies, chosen once in settings.
- Everything is tested against the eBay sandbox first; a `--dry-run` shows exactly what would be sent.
- A local history (SQLite) prevents listing the same scan twice and records eBay listing IDs.

### eBay setup checklist (you)
1. Register at developer.ebay.com (approval can take ~1 business day).
2. Create a Sandbox keyset and a Production keyset.
3. Production keys: claim the "marketplace account deletion" exemption (Lister stores no eBay user data).
4. Create a RuName (redirect URL name) under User Tokens; Lister uses it for a one-time sign-in.
5. Seller Hub: make sure you have shipping, payment and return business policies.
6. Anthropic API key for Phase 2.

Keys go in a local `.env` file that is never committed.

## Watch folder ✅
`src/lister/watcher.py`, `src/lister/pairing.py`, `lister watch`, `lister batches`, `scripts/Lister Watch.bat`.
- Root `C:\Lister` on Windows (inbox / batches / done / problems).
- Pairing: shape match (same spots within ~10 mm after sheet shift, same size within 2%) **and** a
  low-resolution Claude check of which sheet is fronts/backs and whether they're the same cards.
  Measured on the samples: flipping moves cards ≤5 mm; neighbouring cards are 61 mm apart.
- Fronts-only batches when the check fails or no back arrives within 10 minutes.
- Review page lists all batches (`/`), each batch at `/b/<name>/`.

## Phase 5: nice-to-haves
- Price hints from recently sold listings.
- CSV export (eBay bulk upload) as a fallback.
- Watch-folder mode: drop scans in a folder, they show up on the review page.
