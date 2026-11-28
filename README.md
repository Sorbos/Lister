# Lister

Turn trading-card scans (9 cards at a time) into individual, straightened card photos, and, soon,
into eBay listings. See [docs/PLAN.md](docs/PLAN.md) for the roadmap.

## Try it on Windows (no eBay account needed)

1. Install **Python 3.11 or newer** from <https://www.python.org/downloads/>. In the installer,
   tick **"Add python.exe to PATH"**.
2. Open **Command Prompt** and install Lister:
   ```
   py -m pip install https://github.com/Sorbos/Lister/archive/refs/heads/claude/ebay-memorabilia-automation-d25668.zip
   ```
3. Create `C:\Lister\.env` (Notepad is fine; save as "All files", name `.env`) containing:
   ```
   ANTHROPIC_API_KEY=sk-ant-...
   ```
   Without it, Lister still cuts out and pairs cards; it just won't read them.
4. Download `scripts\Lister Watch.bat` from the repo to your desktop and double-click it. A
   browser page opens listing your batches.
5. Point your scanner software at `C:\Lister\inbox` (or copy scans in). Scan 9 fronts, flip,
   scan the backs. Within a minute or so the batch appears; open it to review and approve.
6. When every card in a batch is approved or skipped, the approved cards move to the
   **Inventory** page. Select cards, set prices and staggered start times, and click **Preview**
   to see exactly what would be sent to eBay. Nothing is sent anywhere until eBay is connected.
7. `py -m lister usage` shows what Claude has cost so far.

To update later, run the same `pip install` line again with `--upgrade`.

## Install

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

## Watch folder (the everyday way)

Set your scanner to save into `C:\Lister\inbox`, start Lister, and scan. Each scan is cut into
cards, paired with its back scan, read by Claude, and shows up on the review page by itself.

```
C:\Lister\inbox\      ← scanner saves here
C:\Lister\batches\    ← one folder per batch (cards, details, review state)
C:\Lister\done\       ← original scans, moved here once used
C:\Lister\problems\   ← scans Lister couldn't use, with a .txt note saying why
C:\Lister\.env         ← your keys (ANTHROPIC_API_KEY=..., eBay keys)
```

**Start it:** double-click `scripts\Lister Watch.bat` (copy it to your desktop), or run
`lister watch`. A browser page lists every batch; it refreshes by itself. Close the window to stop.
Scans left in the inbox are picked up next time.

**How fronts and backs are matched:**
- Scans are taken in file-name order (`IMG_..._0045`, then `_0046`), once the scanner has
  finished writing them.
- A scan is paired with the next one only if the cards sit in the same spots with the same
  sizes **and** Claude confirms one is fronts and the other is backs of the same cards (about a
  cent per pair). If you scanned the backs first, they're swapped automatically.
- Otherwise the scan becomes a **fronts-only** batch, and the next scan waits for its own
  partner, so one fronts-only batch can't throw off the pairs after it.
- The last scan waits 10 minutes for its back (`lister watch --wait 5` to change), then goes
  ahead as fronts only.

**Progress:** the Batches page updates by itself every 2 seconds. A panel at the top shows how
many scans are waiting, what Lister is doing right now (matching or cutting out scans), and how far
Claude has got; each batch being read has a progress bar ("Reading card 6 of 9"), and batches
waiting their turn show their place in line. If Lister isn't running, the panel says so.

**Pages:** a bar at the top links **Batches** (every batch; click one to review it), **Inventory**
(price, schedule and list cards), **Listed** (live on eBay; mark sold or send back), **Settings**
(scans folder, wait time) and **Costs** (what Claude has cost: last 7 days, this month, all time,
per batch). It's there whenever Lister runs with `lister watch` or `lister batches`.

**Changing the scans folder:** open **Settings**, type the folder's
full path (e.g. `C:\Users\you\Pictures\Card scans`) and click *Use this folder*. It takes effect
within seconds; set your scanner software to save there too. Lister moves each scan out once used,
so it refuses a whole drive, your home folder or its own folders, and asks before using a folder
that already holds lots of images. The same page sets how long a lone scan waits for its back.

Options: `--root D:\Cards` for another folder, `--no-read` to skip Claude (fill in by hand).
`lister batches` opens the batch list without watching.

**Windows install (once):** install Python 3.11+ from python.org (tick "Add python.exe to PATH"),
then follow *Try it on Windows* above.

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
`out/batch-001/cards.json` with a ready-made eBay title (year, set, number, name), e.g.

    2005-06 Upper Deck Young Guns #201 Sidney Crosby Pittsburgh Penguins RC

Cards without a number get `nno` in its place: `1978 The Muppet Show nno Fozzie`.

Each front is turned so its **picture** is upright (a sideways photo makes the card landscape), and each back so its text is upright. Options: `--model`, `--effort low|medium|high`
(defaults: `claude-opus-5-5`, `medium`; also settable via `LISTER_MODEL` / `LISTER_EFFORT`).
If Claude ever declines a card, the request automatically falls back to another model.

**Cost:** each card's cost shows as it's read, and the run ends with the batch total, e.g.
`Claude cost for this batch: 34.2¢ (3.8¢ per card)`. The review page shows it next to the batch name.
For your running total:

```bash
lister usage
```
```
Claude cost (estimated from list prices; your Anthropic Console has the exact bill)
  Last 7 days:  34.2¢
  This month:   $1.37
  All time:     $4.12  (108 cards, 3.8¢ per card)
```
Every call is logged in `~/.config/lister/usage.jsonl`. These are estimates from Anthropic's list
prices; the Usage and Cost pages in the Anthropic Console show the exact bill.

## Review, then the inventory

**Batches: check the details.** Open a batch (from the Batches page, or `lister review FOLDER` for
a single folder). Each card shows its front and back; fix any field (fields Claude was unsure of
are highlighted ⚠; the title rebuilds as you type), then **Approve** it or tick **Don't list**.
Turn a photo with the ⟲ ⟳ buttons under it, or tick **Select** on several cards and turn
all their fronts (or backs) at once from the bar at the top. When every card is done, the approved cards are copied into the inventory and the batch becomes
read-only. Edits are saved as you go, so you can stop and come back.

**Inventory: price, schedule, list.** Every approved card from every batch, in one place
(`C:\Lister\inventory\<card>\` holds its photos and `card.json`).
- Tick cards (or *Select all*), then under **Set many cards at once** choose **Auction** (start
  price, 1–10 days, optional Buy It Now and reserve) or **Fixed price**, and *Apply to selected*.
- **Staggered endings:** pick when the first card starts and the gap (e.g. 3 minutes), then
  *Stagger selected*. It runs across all selected cards in the order shown, across batches, so
  18 seven-day auctions starting Sun 7:00 PM end 7:00, 7:03, … 7:51 PM a week later.
- Change any single card's title, prices, length, shipping or start time on its row, and add a
  **description for that card** (e.g. "small crease on the back").
- **Shipping:** set a stock shipping cost in Settings; every card without its own cost uses it.
  Override it per card, or for many cards with *Set shipping for selected* (empty = back to stock).
  It's sent to eBay as an override of the first domestic service in your shipping policy, so that
  service needs to be flat-rate.
- **Delete** removes the selected cards from the inventory after a confirmation. They're moved to
  `C:\\Lister\\deleted\\`, not erased, so a mistake can be undone in File Explorer.
- Problems show under each card and block listing (no price, title over 80 characters, Buy It
  Now less than 30% above the start, a start time in the past or more than 3 weeks ahead).
- **Preview** shows exactly what would be sent to eBay; **List on eBay** lists the selected
  cards (with a confirmation), showing progress as it goes.

**Listing description:** each listing's description is the title, then the card's own
description, the card details (year, set, number, condition…), then your **stock description**
from Settings (e.g. how you ship, combined shipping). Settings also holds the defaults new
inventory cards start with (format, starting price, length, fixed price).

**Listed:** cards live or scheduled on eBay, with the eBay item number and end time. Mark
**Sold** (moves to `C:\Lister\sold`) or **Didn't sell → back to inventory** to re-price and
list again (it gets a fresh eBay listing; the old one is kept in the card's history).

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

### Listing
Use the **Inventory** page (above), or from the command line:
```bash
lister list --dry-run          # preview every inventory card; sends nothing
lister list                    # list every inventory card (asks you to confirm)
lister list --ids <card id>…   # just some cards (their folder names in C:\Lister\inventory)
```
For each card Lister uploads the front and back scans to eBay, creates the listing with the
condition (ungraded card condition, or grader + grade), item specifics (player, team, set,
season, card #, rookie…), a description, and your auction or fixed-price settings, publishes it,
and moves the card to `listed`.

- **Scheduled start times** work two ways, chosen in `lister ebay setup`:
  *eBay starts them* (computer can be off; eBay may charge a small scheduling fee) or
  *Lister starts them* (free; keep Lister running until the last card is live).
- **Safe to re-run:** progress is saved in each card's `card.json`. If something fails part-way,
  fix it and list again; cards already listed have moved to `listed`, and a half-done card picks
  up where it stopped, never creating a duplicate listing.
- Card details eBay needs (condition codes, allowed item specific values) are looked up from eBay
  for each category, so they stay current. If eBay requires an item specific Lister doesn't know,
  the listing log tells you its name; add it to `extra_aspects` in `~/.config/lister/<env>/setup.json`.

### Going live
In the developer portal, open your **Production** keyset, respond to the "Marketplace account
deletion" notification requirement by claiming the exemption (Lister stores no eBay user data),
create a production RuName, put the production keys in `.env` with `EBAY_ENV=production`, then
run `lister ebay login` and `lister ebay setup` again with your real seller account.

## Tests

```bash
pytest
```
