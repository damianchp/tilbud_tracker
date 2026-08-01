#!/usr/bin/env python3
"""
Generates synthetic-but-realistic demo data for the Prej-based pipeline:
  1. data/price_history.json   -- 8 prior weeks of prices per product/dealer
  2. scripts/demo_prej_ids.json -- fake canonical_id -> [prej product id]
     mapping, simulating "bootstrap already ran". Only ever read by
     fetch_offers.py --demo; never written into the real data/catalog.json.
  3. scripts/demo_raw_batch.json -- "this run"'s synthetic /v1/products/batch
     response, in the same shape the real Prej API returns, so
     fetch_offers.py --demo exercises the exact same processing code path
     as a real run.

Run once: python3 generate_demo_data.py
Then:     python3 fetch_offers.py --demo

Deterministic (fixed random seed) so re-running produces the same demo.
"""

import json
import random
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
SCRIPTS_DIR = ROOT / "scripts"

random.seed(42)

DEALERS = ["netto", "rema", "foetex", "lidl"]
CHAIN_SLUG = {"netto": "netto", "rema": "rema1000", "foetex": "fotex", "lidl": "lidl"}
CHAIN_NAME = {"netto": "Netto", "rema": "Rema 1000", "foetex": "føtex", "lidl": "Lidl"}

# (canonical_id, prej_id, name, brand, unit, quantity, base_price_dkk)
# base_price is the "normal, no promo" price this product tends to sell at.
PRODUCTS = [
    ("milk-whole",           10001, "Sødmælk 1 l",                    "Arla",     "l",  1,    11.95),
    ("butter-lurpak-salted", 10002, "Smør Saltet 200 g",              "Lurpak",   "g",  200,  24.95),
    ("bread-rye",            10003, "Rugbrød 800 g",                  "Kohberg",  "g",  800,  22.95),
    ("eggs-10",              10004, "Æg 10 stk",                       "Danæg",    "stk", 10,   32.95),
    ("chicken-whole",        10005, "Hel Kylling ca. 1,2 kg",          None,       "kg", 1.2,  54.95),
    ("beef-minced-8-12",     10006, "Hakket Oksekød 8-12% 500 g",      None,       "g",  500,  44.95),
    ("coffee-filter",        10007, "Filterkaffe 400 g",               "Merrild",  "g",  400,  44.95),
    ("oats-rolled",          10008, "Havregryn 1 kg",                  None,       "g",  1000, 16.95),
    ("pasta",                10009, "Spaghetti 500 g",                 None,       "g",  500,  12.95),
    ("rice",                 10010, "Jasminris 1 kg",                  None,       "g",  1000, 19.95),
    ("yoghurt-natural",      10011, "Yoghurt Naturel 1 kg",            None,       "g",  1000, 17.95),
    ("cheese-danbo",         10012, "Danbo 45+ 400 g",                 None,       "g",  400,  39.95),
    ("banana",               10013, "Bananer, løsvægt",                None,       "kg", 1,    14.95),
    ("apple",                10014, "Æbler, Rød/Grøn",                 None,       "kg", 1,    18.95),
    ("tomato",               10015, "Tomater i klase",                 None,       "kg", 1,    24.95),
    ("laundry-liquid",       10016, "Flydende Vaskemiddel Color 1,5 l", None,      "l",  1.5,  59.95),
    ("toilet-paper",         10017, "Toiletpapir 12 ruller",           None,       "stk", 12,   42.95),
    ("dishwasher-tabs",      10018, "Opvasketabs All in One 40 stk",   None,       "stk", 40,   69.95),
]

# Per-dealer relative price index (dealers aren't identical even off-promo)
DEALER_INDEX = {"netto": 1.00, "rema": 0.98, "foetex": 1.06, "lidl": 0.94}

TODAY = date.today()
WEEKS_OF_HISTORY = 8


def unit_price_basis(unit: str) -> str:
    if unit in ("g", "kg"):
        return "kr/kg"
    if unit in ("ml", "l"):
        return "kr/l"
    return "kr/stk"


def compute_unit_price(price_dkk: float, unit: str, quantity: float) -> float:
    factor = {"g": 0.001, "kg": 1.0, "ml": 0.001, "l": 1.0}.get(unit)
    if factor is None or not quantity:
        return round(price_dkk, 2)  # stk-based: unit price == item price
    return round(price_dkk / (quantity * factor), 2)


def build_history():
    """8 weeks of history, most weeks near the base price, with occasional
    genuine promos (dip) so deal_quality has something real to discriminate."""
    history = {}
    for cid, _pid, _name, _brand, unit, qty, base_price in PRODUCTS:
        history[cid] = []
        for w in range(WEEKS_OF_HISTORY, 0, -1):
            wk_date = TODAY - timedelta(weeks=w)
            n_dealers_this_week = random.choice([1, 2, 2, 3, 4])
            dealers_this_week = random.sample(DEALERS, n_dealers_this_week)
            for dealer in dealers_this_week:
                idx = DEALER_INDEX[dealer]
                roll = random.random()
                if roll < 0.15:
                    price = round(base_price * idx * random.uniform(0.55, 0.75), 2)  # real promo
                elif roll < 0.30:
                    price = round(base_price * idx * random.uniform(0.90, 0.97), 2)  # weak "offer"
                else:
                    price = round(base_price * idx * random.uniform(0.97, 1.03), 2)  # near-normal
                unit_price = compute_unit_price(price, unit, qty)
                history[cid].append({
                    "date": wk_date.isoformat(),
                    "dealer": dealer,
                    "price": price,
                    "unit_price": unit_price,
                })
    return history


def build_demo_batch():
    """This run's synthetic Prej /v1/products/batch response. Curated
    storyline so the demo has a mix of excellent/good/normal deals and at
    least one item completely absent from a couple of chains."""
    products = []

    story = {
        "beef-minced-8-12": {"netto": 0.60, "rema": 0.62, "foetex": None, "lidl": 0.64},   # excellent, stock-up
        "laundry-liquid":   {"netto": None, "rema": 0.58, "foetex": 0.97, "lidl": None},   # excellent at Rema only
        "butter-lurpak-salted": {"netto": 0.76, "rema": 0.80, "foetex": 1.0, "lidl": 0.74}, # Lidl actually cheapest here
        "coffee-filter":    {"netto": 0.95, "rema": None, "foetex": 0.93, "lidl": 0.90},   # weak/fake-ish offer
        "milk-whole":       {"netto": 1.0, "rema": 0.98, "foetex": 1.04, "lidl": 0.95},    # normal, always-on
    }

    for cid, pid, name, brand, unit, qty, base_price in PRODUCTS:
        overrides = story.get(cid, {})
        prices = []
        for dealer in DEALERS:
            idx = DEALER_INDEX[dealer]
            if cid in story:
                mult = overrides.get(dealer)
                if mult is None:
                    continue  # not carried / not seen at this dealer right now
            else:
                if random.random() < 0.15:
                    continue
                mult = random.uniform(0.85, 1.02)
            price_dkk = round(base_price * idx * mult, 2)
            unit_price_dkk = compute_unit_price(price_dkk, unit, qty)
            offer_ends = None
            if mult < 0.9:
                offer_ends = (TODAY + timedelta(days=random.choice([2, 4, 6]))).isoformat()
            prices.append({
                "chain_slug": CHAIN_SLUG[dealer],
                "chain_name": CHAIN_NAME[dealer],
                "chain_logo_url": None,
                "image_url": None,
                "unit": unit if unit != "stk" else None,
                "quantity": qty if unit != "stk" else None,
                "price": round(price_dkk * 100),          # øre
                "unit_price": round(unit_price_dkk * 100), # øre
                "last_seen_date": TODAY.isoformat(),
                "ai_matched": False,
                "source": "flyer" if offer_ends else "scraped",
                "offer_ends": offer_ends,
                "quantity_label": f"{qty} {unit}" if unit != "stk" else f"{qty} stk",
                "unit_price_label": f"{unit_price_dkk:.2f} {unit_price_basis(unit)}",
            })
        products.append({
            "id": pid,
            "name": name,
            "description": None,
            "brand": brand,
            "image_url": None,
            "unit": unit if unit != "stk" else None,
            "quantity": qty if unit != "stk" else None,
            "organic": False,
            "chain_count": len(prices),
            "category_name": None,
            "category_slug": None,
            "subcategory_name": None,
            "subcategory_slug": None,
            "sub_subcategory_name": None,
            "sub_subcategory_slug": None,
            "quantity_label": f"{qty} {unit}" if unit != "stk" else f"{qty} stk",
            "unit_price_label": None,
            "prices": prices,
        })
    return {"products": products}


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    history = build_history()
    with open(DATA_DIR / "price_history.json", "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)
        f.write("\n")

    prej_ids = {cid: [pid] for cid, pid, *_ in PRODUCTS}
    with open(SCRIPTS_DIR / "demo_prej_ids.json", "w", encoding="utf-8") as f:
        json.dump(prej_ids, f, ensure_ascii=False, indent=2)
        f.write("\n")

    batch = build_demo_batch()
    with open(SCRIPTS_DIR / "demo_raw_batch.json", "w", encoding="utf-8") as f:
        json.dump(batch, f, ensure_ascii=False, indent=2)
        f.write("\n")

    n_hist = sum(len(v) for v in history.values())
    n_prices = sum(len(p["prices"]) for p in batch["products"])
    print(f"Wrote {n_hist} historical price points across {len(history)} products "
          f"({WEEKS_OF_HISTORY} weeks) to data/price_history.json")
    print(f"Wrote {len(prej_ids)} fake product-id mappings to scripts/demo_prej_ids.json")
    print(f"Wrote {n_prices} synthetic current prices across {len(batch['products'])} "
          f"products to scripts/demo_raw_batch.json")
    print("Next: python3 fetch_offers.py --demo")


if __name__ == "__main__":
    main()
