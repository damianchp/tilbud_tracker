#!/usr/bin/env python3
"""
TilbudTracker fetch pipeline (Prej API edition)
=================================================
Source: https://prej.dk/api — a documented, free-tier REST API for Danish
grocery prices (25 requests/day on the free plan). This replaced an earlier
design built on reverse-engineered Tjek/eTilbudsavis traffic; Prej is a
proper first-party API with normalized unit prices, so a lot of the old
parsing logic (Danish multi-buy regex, unit conversion) simply isn't needed
anymore — Prej already returns unit_price in øre per kg/L/stk.

Two-phase design, both cheap on the 25/day free tier:
  1. BOOTSTRAP (self-resuming, budget-limited): for each catalog product
     that doesn't yet have a known Prej product id, search for it
     (GET /v1/products?q=...) and store the matched id(s) back into
     data/catalog.json. Capped per run (config/dealers.json:
     max_calls_per_run) so it spreads over a few runs rather than blowing
     the daily quota in one go.
  2. DAILY REFRESH (1 call, regardless of how many products you track):
     GET /v1/products/batch?ids=... fetches current prices for every
     already-known product, across every chain that carries it, in a
     single request (up to 500 ids).

No "was" / "pre_price" field exists in Prej's data, so deal quality and
savings are both computed from OUR OWN accumulated price_history.json
(percentile-based once there are 6+ observations) rather than trusting a
retailer-supplied "was" price, which is more robust anyway.

Usage:
    python3 fetch_offers.py --demo     # offline dry run, bundled synthetic data
    python3 fetch_offers.py --live     # real run (needs PREJ_API_KEY env var)

Reads:
    config/dealers.json
    data/catalog.json                 (prej_ids get added/updated in place)
    data/price_history.json           (created empty on first run)
    scripts/demo_raw_batch.json        \\ demo mode only
    scripts/demo_prej_ids.json         /

Writes:
    data/offers.json
    data/price_history.json           (updated in place, capped at 52 weeks)
    data/catalog.json                 (prej_ids filled in as bootstrap runs; --live only)

Dependencies: Python 3.9+ standard library only. No pip install needed.
"""

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"
SCRIPTS_DIR = ROOT / "scripts"

API_BASE = "https://api.prej.app"
HISTORY_WEEKS_CAP = 52
MIN_MATCH_TOKEN_LEN = 3


def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[{ts}] {msg}", file=sys.stderr)


# ---------------------------------------------------------------------------
# I/O helpers
# ---------------------------------------------------------------------------

def load_json(path: Path, default):
    if not path.exists():
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.write("\n")


# ---------------------------------------------------------------------------
# Text normalization (for picking a confident search match, not for
# substring-matching a raw feed the way the old Tjek pipeline had to)
# ---------------------------------------------------------------------------

_DIACRITIC_MAP = str.maketrans({"æ": "ae", "ø": "oe", "å": "aa", "Æ": "ae", "Ø": "oe", "Å": "aa"})


def normalize_name(raw: str) -> str:
    if not raw:
        return ""
    s = raw.lower().translate(_DIACRITIC_MAP)
    s = re.sub(r"[^a-z0-9æøå\s%]", " ", s, flags=re.UNICODE)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def unit_price_basis_from_unit(unit: str | None) -> str:
    if unit in ("g", "kg"):
        return "kr/kg"
    if unit in ("ml", "l", "cl"):
        return "kr/l"
    return "kr/stk"


# ---------------------------------------------------------------------------
# Prej API client
# ---------------------------------------------------------------------------

class CallBudget:
    """Tracks how many API calls this run has made against the configured
    per-run cap, so bootstrap search never crowds out the batch refresh."""
    def __init__(self, max_calls: int):
        self.max_calls = max_calls
        self.used = 0

    def remaining(self) -> int:
        return max(0, self.max_calls - self.used)

    def spend(self, n: int = 1) -> None:
        self.used += n


def api_get(path: str, params: dict, api_key: str, budget: CallBudget,
            max_retries: int, delay_s: float):
    if budget.remaining() <= 0:
        log(f"  call budget exhausted for this run, skipping GET {path}")
        return None
    query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    url = f"{API_BASE}{path}?{query}" if query else f"{API_BASE}{path}"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {api_key}",
        "User-Agent": "TilbudTracker/1.0 (personal use)",
        "Accept": "application/json",
    })
    last_err = None
    for attempt in range(1, max_retries + 1):
        try:
            budget.spend(1)
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise RuntimeError("401 Unauthorized — check PREJ_API_KEY") from e
            if e.code == 429:
                log("  429 rate limited by Prej — stopping further calls this run")
                budget.used = budget.max_calls  # stop spending further this run
                return None
            last_err = e
        except (urllib.error.URLError, TimeoutError) as e:
            last_err = e
        log(f"  request failed (attempt {attempt}/{max_retries}): {last_err}")
        time.sleep(delay_s * (2 ** (attempt - 1)))
    log(f"  GET {path} failed after {max_retries} attempts: {last_err}")
    return None


def search_products(query: str, api_key: str, budget: CallBudget, max_retries: float, delay_s: float):
    resp = api_get("/v1/products", {"q": query, "limit": 5}, api_key, budget, max_retries, delay_s)
    if resp is None:
        return None
    return resp.get("products", [])


def batch_fetch(ids: list, api_key: str, budget: CallBudget, max_retries: int, delay_s: float):
    """Chunked defensively at 500 ids/call (Prej's documented max), though a
    typical household catalog (dozens of products) fits in a single chunk."""
    all_products = []
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        resp = api_get("/v1/products/batch", {"ids": ",".join(str(x) for x in chunk)},
                        api_key, budget, max_retries, delay_s)
        if resp is None:
            continue
        all_products.extend(resp.get("products", []))
    return all_products


# ---------------------------------------------------------------------------
# Bootstrap: discover a Prej product id per catalog product
# ---------------------------------------------------------------------------

def pick_confident_match(query: str, exclude_terms: list, results: list):
    """Requires at least one real query token (len >= 3) to actually appear
    in the candidate's name/brand, and rejects anything hitting an
    exclude_term. This is deliberately conservative — a wrong auto-match is
    worse than no match, since a wrong match silently shows the wrong
    product's price."""
    qnorm = normalize_name(query)
    tokens = [t for t in qnorm.split() if len(t) >= MIN_MATCH_TOKEN_LEN]
    if not tokens:
        return None
    for p in results:
        name_norm = normalize_name(f"{p.get('name') or ''} {p.get('brand') or ''}")
        if any(normalize_name(ex) and normalize_name(ex) in name_norm for ex in exclude_terms):
            continue
        if any(tok in name_norm for tok in tokens):
            return p
    return None


def run_bootstrap(catalog: dict, api_key: str, budget: CallBudget, max_retries: int, delay_s: float) -> int:
    """Fills in `prej_ids` for catalog products that don't have any yet.
    Mutates `catalog` in place. Returns the number of products newly
    mapped this run (0 in --demo mode, which never calls this)."""
    newly_mapped = 0
    for product in catalog.get("products", []):
        if product.get("prej_ids"):
            continue
        if budget.remaining() <= 1:  # always leave at least 1 call for the batch refresh
            break
        terms = product.get("match_terms") or [product.get("display_name", "")]
        query = terms[0]
        results = search_products(query, api_key, budget, max_retries, delay_s)
        if not results:
            log(f"  bootstrap: no results for '{query}' ({product['canonical_id']})")
            continue
        match = pick_confident_match(query, product.get("exclude_terms") or [], results)
        if match:
            product["prej_ids"] = [match["id"]]
            newly_mapped += 1
            log(f"  bootstrap: matched '{query}' -> #{match['id']} \"{match.get('name')}\" "
                f"({product['canonical_id']})")
        else:
            log(f"  bootstrap: no confident match for '{query}' among "
                f"{[r.get('name') for r in results]} ({product['canonical_id']})")
    return newly_mapped


# ---------------------------------------------------------------------------
# Demo fetch (offline, deterministic)
# ---------------------------------------------------------------------------

def load_demo_batch() -> list:
    demo = load_json(SCRIPTS_DIR / "demo_raw_batch.json", {"products": []})
    return demo.get("products", [])


def apply_demo_prej_ids(catalog: dict) -> None:
    """Demo mode simulates 'bootstrap already ran' by loading a fixed
    canonical_id -> prej id mapping, WITHOUT writing anything into the real
    data/catalog.json (that file ships clean, with no prej_ids, for real
    deployments)."""
    mapping = load_json(SCRIPTS_DIR / "demo_prej_ids.json", {})
    by_cid = {p["canonical_id"]: p for p in catalog.get("products", [])}
    for cid, ids in mapping.items():
        if cid in by_cid:
            by_cid[cid]["prej_ids"] = ids


# ---------------------------------------------------------------------------
# Core processing (shared by live and demo modes)
# ---------------------------------------------------------------------------

def process_batch(products: list, catalog: dict, dealer_keys_by_slug: dict,
                   price_history: dict, today: date, week_label: str) -> tuple[dict, dict]:
    """Turns Prej ProductSummary objects (with their prices[] arrays) into
    our normalized offers.json + updates price_history.json in place."""
    id_to_cid = {}
    for p in catalog.get("products", []):
        for pid in p.get("prej_ids") or []:
            id_to_cid[pid] = p["canonical_id"]

    normalized_offers = []
    seen_this_run = set()  # (date, dealer, canonical_id) -> dedupe re-runs same day

    for product in products:
        cid = id_to_cid.get(product.get("id"))
        if not cid:
            continue  # a batch id we don't recognize anymore (shouldn't normally happen)
        for price in product.get("prices") or []:
            dealer_key = dealer_keys_by_slug.get(price.get("chain_slug"))
            if not dealer_key:
                continue  # a chain we're not tracking (Prej covers 18+, we track 3)
            price_dkk = (price.get("price") or 0) / 100
            unit_price_dkk = (price["unit_price"] / 100) if price.get("unit_price") is not None else None
            basis = unit_price_basis_from_unit(price.get("unit"))
            flags = []
            if price.get("offer_ends"):
                flags.append(f"offer_ends:{price['offer_ends']}")
            entry = {
                "offer_id": f"prej-{product['id']}-{dealer_key}",
                "dealer": dealer_key,
                "name_raw": product.get("name") or "",
                "canonical_id": cid,
                "price": price_dkk,
                "pre_price": None,  # Prej doesn't provide a "was" price; see module docstring
                "unit_count": 1,
                "size_value": price.get("quantity"),
                "size_unit": price.get("unit"),
                "unit_price": unit_price_dkk if unit_price_dkk is not None else price_dkk,
                "unit_price_basis": basis,
                "valid_from": None,
                "valid_to": price.get("offer_ends"),
                "flags": flags,
                "image": price.get("image_url") or product.get("image_url"),
                "source": price.get("source"),
                "last_seen_date": price.get("last_seen_date"),
            }
            normalized_offers.append(entry)

            key = (today.isoformat(), dealer_key, cid)
            if key not in seen_this_run:
                seen_this_run.add(key)
                price_history.setdefault(cid, []).append({
                    "date": today.isoformat(),
                    "dealer": dealer_key,
                    "price": price_dkk,
                    "unit_price": entry["unit_price"],
                })

    for cid, points in price_history.items():
        points.sort(key=lambda p: p["date"])
        cap = HISTORY_WEEKS_CAP * 7 * 3  # ~52 weeks * ~daily runs * 3 chains, generous cap
        if len(points) > cap:
            price_history[cid] = points[-cap:]

    offers_doc = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "week": week_label,
        "dealers_failed": [],
        "offers": normalized_offers,
    }
    return offers_doc, price_history


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def iso_week_label(d: date) -> str:
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def main():
    import os

    parser = argparse.ArgumentParser(description="TilbudTracker fetch pipeline (Prej API)")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--demo", action="store_true", help="offline run using bundled synthetic data")
    mode.add_argument("--live", action="store_true", help="real run against the Prej API")
    args = parser.parse_args()

    dealers_cfg_full = load_json(CONFIG_DIR / "dealers.json", {})
    dealers_cfg = dealers_cfg_full.get("dealers", [])
    dealer_keys_by_slug = {d["prej_chain_slug"]: d["key"] for d in dealers_cfg}
    max_calls = dealers_cfg_full.get("max_calls_per_run", 20)
    max_retries = dealers_cfg_full.get("max_retries", 3)
    delay_s = dealers_cfg_full.get("request_delay_seconds", 0.5)

    catalog = load_json(DATA_DIR / "catalog.json", {"products": []})
    price_history = load_json(DATA_DIR / "price_history.json", {})
    today = date.today()
    week_label = iso_week_label(today)
    budget = CallBudget(max_calls)

    if args.demo:
        log("Running in --demo mode (no network calls, using bundled synthetic data)")
        apply_demo_prej_ids(catalog)  # in-memory only, never written to disk in demo mode
        products = load_demo_batch()
        log(f"  loaded {len(products)} synthetic products from demo_raw_batch.json")
    else:
        api_key = os.environ.get("PREJ_API_KEY", "")
        if not api_key:
            sys.exit("PREJ_API_KEY env var required for --live. Get a free key at https://prej.dk/api")

        newly_mapped = run_bootstrap(catalog, api_key, budget, max_retries, delay_s)
        if newly_mapped:
            save_json(DATA_DIR / "catalog.json", catalog)
            log(f"  bootstrap mapped {newly_mapped} new product(s), saved to data/catalog.json")

        all_ids = sorted({pid for p in catalog.get("products", []) for pid in (p.get("prej_ids") or [])})
        if not all_ids:
            log("  no products have a known Prej id yet — nothing to fetch this run "
                "(bootstrap will keep resuming on future runs until the budget allows more)")
            products = []
        else:
            log(f"  fetching current prices for {len(all_ids)} known product id(s)...")
            products = batch_fetch(all_ids, api_key, budget, max_retries, delay_s)
            log(f"  batch returned {len(products)} product(s)")

    offers_doc, price_history = process_batch(
        products, catalog, dealer_keys_by_slug, price_history, today, week_label)

    save_json(DATA_DIR / "offers.json", offers_doc)
    save_json(DATA_DIR / "price_history.json", price_history)
    # kept for schema compatibility with the app's (currently unused, always-empty)
    # "recognized but not tracked" review list — see README for why this is empty now
    save_json(DATA_DIR / "unmatched_candidates.json",
              {"generated_at": offers_doc["generated_at"], "candidates": []})

    matched_cids = {o["canonical_id"] for o in offers_doc["offers"]}
    log(f"Done. {len(offers_doc['offers'])} price points across {len(matched_cids)} products, "
        f"{budget.used}/{budget.max_calls} API calls used this run.")


if __name__ == "__main__":
    main()
