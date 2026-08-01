# TilbudTracker

Grocery price aggregator for Netto, Rema 1000, føtex, and Lidl. Tracks the
products your household actually buys, ranks the cheapest chain per item,
builds a per-chain shopping basket, and tells you whether a given price is
actually good or just a normal one — based on your own accumulated price
history, not a retailer's "was" price.

Data comes from [Prej](https://prej.dk/api), a free, documented API for
Danish grocery prices. *(An earlier version of this project chased
reverse-engineered eTilbudsavis/Tjek traffic instead — Prej is a much
better foundation: a real signup, normalized unit prices, and everyday
shelf prices rather than only weekly promotional catalog items.)*

**365discount isn't tracked.** Coop, which owns it, doesn't have a public
product feed yet, so Prej lists the chain but carries zero products for it.
You can still track a 365discount purchase manually — tap **Ran out?**,
search or type the item, and assign it to that chain by hand; it just won't
get an automatic price.

**Right now, opening `index.html` shows sample/demo data** (clearly labeled
in-app) so you can try every feature immediately. Follow **One-time setup**
below to switch it over to your own real, live prices.

---

## Try it immediately (no setup)

Just open `index.html` in a browser. It ships with 8 weeks of synthetic
price history and a synthetic "current prices" snapshot so every feature —
deal-quality badges, the budget prioritizer, the basket consolidation
suggestion, sparklines — has something real to show you. A banner tells you
when you're looking at demo data.

To generate a *fresh* batch of synthetic demo data:
```
python3 scripts/generate_demo_data.py
python3 scripts/fetch_offers.py --demo
```
This regenerates `data/offers.json` and `data/price_history.json` — but
**does not** update the copy embedded inside `index.html` (that's only a
fallback for when `data/*.json` can't be fetched, e.g. previewing the file
standalone). Deploy to GitHub Pages and the app fetches the live files
directly; the embedded copy stops mattering.

---

## One-time setup (real prices)

### 1. Get a free Prej API key
Go to **[prej.dk/api](https://prej.dk/api)** → **Gratis (Free)** plan →
accept the terms → **Få nøgle** ("Get key"). The key is issued instantly,
no email approval wait. Free tier: 25 requests/day, non-commercial use —
plenty for this (steady-state usage is about **1 request/day**; see
*How it stays inside the free tier* below).

### 2. Try a real run locally
```
export PREJ_API_KEY="prej_..."
python3 scripts/fetch_offers.py --live
```
First run "bootstraps": for each of the ~40 products in
`data/catalog.json`, it searches Prej for a matching product and saves the
match back into `data/catalog.json` as `prej_ids`. This is capped at
`max_calls_per_run` (20, in `config/dealers.json`) so it won't blow the
daily quota — if you have more products than that fits in one run, it just
picks up where it left off on the next run. Check `data/offers.json`
afterwards; every matched product should show real current prices.

If a product's `prej_ids` never gets filled in (check the log output —
it'll say `no confident match`), the product probably needs a better search
term. Edit its `match_terms` in `data/catalog.json` and re-run.

### 3. Deploy to GitHub Pages
1. Push this repo to GitHub.
2. Repo Settings → Secrets and variables → Actions → New repository secret:
   name `PREJ_API_KEY`, value from step 1.
3. Repo Settings → Pages → Deploy from branch → `main` / root.
4. `.github/workflows/fetch.yml` runs daily and commits fresh data
   automatically; trigger it manually from the Actions tab any time.
5. Open `https://<you>.github.io/<repo>/` — same app, now backed by real
   prices. The demo banner disappears once `data/*.json` fetches
   successfully.

### How it stays inside the free tier
- **Bootstrap** (discovering a Prej id for a new product): 1 API call per
  new product, capped at 20/run, self-resuming across runs.
- **Daily refresh**: **1 API call, total** — `/v1/products/batch` fetches
  current prices for every already-known product across every chain that
  carries it, in a single request (up to 500 ids). Whether you track 10
  products or 100, it's still one call.
- Price *history* costs nothing extra — each day's batch response is
  appended to `data/price_history.json`, which is what deal-quality scoring
  reads from. No separate history calls needed for the steady state.

---

## Using the app

- **This Week** — your favorite products, cheapest chain first, with a
  stamped quality badge (*Best price / Good price / Normal price / Rarely
  this cheap*) based on 52 weeks of your own accumulated price history —
  not a retailer-supplied discount percentage. Set a weekly budget and it
  flags when recurring staples alone exceed it, or suggests extra stock-up
  buys that fit inside what's left.
- **Basket** — this week's shopping list grouped by chain, with totals,
  savings vs. your own historical median (not a marked-up "was" price), and
  a nudge if skipping a chain with a small subtotal saves a store visit for
  only a few kroner more elsewhere.
- **My Products** — what you're tracking and how often you buy it
  (auto-tunes itself from purchase history over time), plus a place to add
  products the shared catalog doesn't know about.
- **History** — spend per week per chain, and total money saved since you
  started tracking.
- **Ran out?** (floating button, every tab) — add something to the basket
  right now, independent of the weekly cycle. Search finds anything in the
  shared catalog or your own tracked products; anything else can be added
  as a one-off ("engangsvare"), landing in an "Andet" basket group you can
  reassign to a specific chain once you know where you'll buy it.

First time you open it with no products picked, you get a two-step picker:
tap what your household buys, then say roughly how often. Everything after
that (cadence, "due" status) tunes itself from what you mark as bought.

Your data (favorites, purchase log, budget, custom products) lives in the
browser's `localStorage` only — nothing is sent anywhere except the price
requests to Prej, which run server-side in GitHub Actions, not from your
browser. Use **Export my data** / **Import** at the bottom of the app to
back it up or move it to another device. Switch the app's language with the
**EN / DA** toggle in the header — product names stay Danish either way
(that's the literal text used at the till), only the app's own labels
translate.

---

## How it's built

```
tilbud-tracker/
├── index.html                     the whole app: one file, no build step,
│                                   no framework, no external JS dependency
├── config/dealers.json            the 4 chains + their Prej chain slugs
├── data/
│   ├── catalog.json                shared matching dictionary (~40 Danish
│   │                                grocery staples) — committed to the
│   │                                repo, not personal. Gains a prej_ids
│   │                                field per product as bootstrap runs.
│   ├── offers.json                 current prices, normalized
│   ├── price_history.json          rolling price log per product
│   └── unmatched_candidates.json   unused under the Prej pipeline, kept
│                                    empty for schema stability
├── scripts/
│   ├── fetch_offers.py             the pipeline: bootstrap (discover Prej
│   │                                ids) → batch fetch → normalize →
│   │                                update history. stdlib only.
│   ├── generate_demo_data.py       makes the synthetic demo dataset
│   ├── demo_prej_ids.json          synthetic id mapping for --demo
│   └── demo_raw_batch.json         synthetic /v1/products/batch response
└── .github/workflows/fetch.yml     daily scheduled run + auto-commit
```

**Design choice worth knowing about:** the Python pipeline only computes
the *objective* stuff — what things cost, where, and how that compares to
history. It deliberately does **not** know what's "due", build your basket,
or apply your budget, because that depends on your purchase history, which
only exists in your browser's `localStorage`, not in a repo any pipeline
run can see. All of that personalization runs client-side in `index.html`,
recomputed instantly from `offers.json` + `price_history.json` +
`catalog.json` + your local state every time the app loads. Adding a custom
product (My Products tab → *Add your own product*) works immediately —
the app re-matches this week's unmatched entries against your own search
terms in the browser — though for genuinely new products with no Prej
match at all, prices won't populate until the pipeline's next bootstrap
run picks them up.

**Deal-quality scoring:** once a product has 6+ historical price points,
its current price is scored by percentile against its own trailing history
(any of the tracked chains) — ≤10th percentile = *Best price*, ≤35th = *Good
price*, ≤65th = *Normal price*, else *Rarely this cheap*. Before there's
enough history (a few days after first tracking something), it falls back
to whether Prej flagged the price with an offer end date at all.

---

## Extending it

1. **365discount coverage** — check `GET /v1/chains` periodically; if Coop
   opens a public feed and Prej picks it up, add `"365": {...}` back to
   `config/dealers.json` and the app picks it up with no other changes.
2. **Email/notification digest** — a small addition to the Actions workflow
   that renders `offers.json` into an email when prices refresh.
3. **Google Drive sync of your local state** — so favorites/purchases follow
   you across devices instead of living in one browser's `localStorage`.
4. **Meal suggestions** — an optional Claude API call from the client
   turning this week's best-priced proteins/produce into a few dinner ideas.
5. **`/v1/products/optimize`** — Prej has a built-in cross-store basket
   optimizer (`GET /v1/products/optimize?ids=...`) that could complement or
   replace parts of the app's own basket-building logic; worth a look if
   you want to lean on it instead.

---

## Notes

- Personal, non-commercial use, well inside Prej's free-tier rate limit
  (see *How it stays inside the free tier* above).
- Not affiliated with Netto, Rema 1000, føtex, Salling Group, or Prej.
- If the API key stops working or a product's match goes stale, `fetch_offers.py
  --live` fails loudly (non-zero exit on total failure, visible in the
  Actions run log) rather than silently serving stale data.
