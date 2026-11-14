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

## List on eBay

### One-time setup
1. **Developer account:** register at <https://developer.ebay.com> (approval can take about a day).
2. **Keys:** under *Application Keys*, create a **Sandbox** keyset (and later a **Production** one).
   Note the *App ID (Client ID)* and *Cert ID (Client Secret)*.
3. **Sign-in setting (RuName):** under *User Tokens* → *Get a Token from eBay via Your Application*,
   add an eBay Redirect URL. Keep eBay's default "auth accepted" page. Note the **RuName** value
   (looks like `Your_Name-YourApp-SBX-abc-defgh`).
4. **Sandbox test seller:** for testing, create a sandbox user at *Sandbox user* in the developer
   portal and sign in with that instead of your real account.
5. Create a `.env` file in the folder you run Lister from (it's git-ignored):
   ```
   EBAY_ENV=sandbox            # switch to production when ready
   EBAY_MARKETPLACE=EBAY_US    # or EBAY_CA, EBAY_GB, EBAY_AU
   EBAY_CLIENT_ID=...
   EBAY_CLIENT_SECRET=...
   EBAY_RUNAME=...
   ANTHROPIC_API_KEY=...
   ```
6. Connect and choose your settings:
   ```bash
   lister ebay login    # opens eBay; approve, then paste the address you land on
   lister ebay setup    # pick shipping/payment/return policies, item location, scheduling mode
   lister ebay status   # check everything is set
   ```
   You stay signed in for about 18 months. Sign-in tokens are stored in `~/.config/lister/`,
   readable only by you.

### Listing a batch
```bash
lister list out/batch-001 --dry-run   # shows exactly what would be sent, sends nothing
lister list out/batch-001             # lists every approved card (asks you to confirm)
```
For each approved card Lister uploads the front and back scans to eBay, creates the listing with
the condition (ungraded card condition, or grader + grade), item specifics (player, team, set,
season, card #, rookie…), a description, and your auction or fixed-price settings, then publishes it.

- **Scheduled start times** (from the review page) work two ways, chosen in `lister ebay setup`:
  *eBay starts them* (computer can be off; eBay may charge a small scheduling fee) or
  *Lister starts them* (free; leave `lister list` running until the last card is live).
- **Safe to re-run:** progress is saved in `ebay.json` in the batch folder. If something fails
  part-way, fix it and run the same command again; cards already listed are skipped, never duplicated.
- Card details eBay needs (condition codes, allowed item specific values) are looked up from eBay
  for each category, so they stay current. If eBay requires an item specific Lister doesn't know,
  `lister list` tells you its name; add it to `extra_aspects` in `~/.config/lister/<env>/setup.json`.

### Going live
In the developer portal, open your **Production** keyset, respond to the "Marketplace account
deletion" notification requirement by claiming the exemption (Lister stores no eBay user data),
create a production RuName, put the production keys in `.env` with `EBAY_ENV=production`, then
run `lister ebay login` and `lister ebay setup` again with your real seller account.

## Tests

```bash
pytest
```
