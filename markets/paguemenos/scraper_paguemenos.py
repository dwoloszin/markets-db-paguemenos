"""
scraper_paguemenos.py - Super Pague Menos (https://www.superpaguemenos.com.br)

Platform : VTEX IO, classic catalog API + checkout simulation.
Store    : PRICES ARE PER STORE (measured 2026-10-27: 10-33% of 200 sampled products differ
           between sellers). /api/checkout/pub/regions returned the SAME 22 white-label sellers
           (superpaguemenos<nn>) for all four CEPs tested, sellers are named only by code, and the
           pickup-point list is empty, so a CEP cannot choose a store. One fixed seller is
           scraped -> store_id is the seller id, not a physical address.
Listing  : catalog_system products/search per category segment (~17k products): name, ean, brand,
           image, url, sku id. The catalog price belongs to seller 1, NOT to the store, so it is
           not used.
Prices   : POST /api/checkout/pub/orderForms/simulation with up to 50 {id, seller} items ->
           listPrice / sellingPrice / availability for that seller. The default CEP is outside the
           delivery area, so availability is often "cannotBeDelivered" but prices still come back.
           Only "withoutStock" counts as unavailable.
Barcode  : inline items[0].ean, 89.8% valid GTIN (GTIN-8 rejected). Many butchery items carry short
           PLU codes, and some popular items (Heineken 350ml, Sprite) have an empty ean on every
           source checked.
Runtime  : ~23 min for ~16.9k products.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from markets.common.geo import format_zip
from markets.common.gtin import first_gtin
from markets.common.http import make_session, post_json
from markets.common.offer import make_offer
from markets.common.vtex import VtexCatalog

STORE_KEY = "paguemenos"
BASE_URL = "https://www.superpaguemenos.com.br"
SELLER = "superpaguemenos03"
SIM_BATCH = 50


def _simulate(session, skus: List[str], zip_code: str) -> Dict[str, Dict[str, Any]]:
    body = {"items": [{"id": s, "quantity": 1, "seller": SELLER} for s in skus],
            "postalCode": format_zip(zip_code), "country": "BRA"}
    data = post_json(session, f"{BASE_URL}/api/checkout/pub/orderForms/simulation?sc=1",
                     json_body=body, log_prefix="[paguemenos] ") or {}
    return {str(i.get("id")): i for i in data.get("items") or [] if isinstance(i, dict)}


def _offer(product: Dict[str, Any], sim: Dict[str, Any], label: str) -> Optional[Dict[str, Any]]:
    item = (product.get("items") or [{}])[0]
    listp, sellp = sim.get("listPrice"), sim.get("sellingPrice")
    if not sellp:
        return None
    # Weighed items: listPrice is per kg while sellingPrice is for one whole piece (a 2 kg papaya
    # has unitMultiplier 2 and sellingPrice 2 x listPrice). Store the price per measurementUnit,
    # so "kg" rows compare correctly with other markets.
    try:
        mult = float(sim.get("unitMultiplier") or 1) or 1.0
    except (TypeError, ValueError):
        mult = 1.0
    unit_price = sellp / mult
    regular = (listp or unit_price) / 100.0
    promo = round(unit_price) / 100.0 if listp and unit_price < listp - 1 else None
    avail = sim.get("availability")
    images = item.get("images") or []
    link_text = product.get("linkText")
    return make_offer(
        product_id=product.get("productId"), store_id=SELLER, product_name=product.get("productName"),
        regular_price=regular, promo_price=promo,
        barcode=first_gtin(item.get("ean"), allow_gtin8=False), barcode_source="inline",
        brand=product.get("brand"), category_path=label, unit=sim.get("measurementUnit"),
        is_available=(avail != "withoutStock") if avail else None,
        product_url=product.get("link") or (f"{BASE_URL}/{link_text}/p" if link_text else None),
        image_url=images[0].get("imageUrl") if images and isinstance(images[0], dict) else None,
    )


def scrape(db, zip_code: str, limit: Optional[int] = None) -> Dict[str, int]:
    session = make_session({"Referer": BASE_URL + "/", "Origin": BASE_URL})
    db.save_store_info(SELLER, query_zip=format_zip(zip_code), name=f"Super Pague Menos ({SELLER})", state="SP")
    catalog = VtexCatalog(session, BASE_URL, delay=0.2, log_prefix="[paguemenos] ")
    segments = catalog.segments(depth=3) or [(None, "all")]
    print(f"[paguemenos] {len(segments)} segments, prices via simulation for seller {SELLER}")
    seen: set = set()
    total = {"upserted": 0, "skipped": 0, "with_barcode": 0}
    for fq, label in segments:
        products: List[Dict[str, Any]] = []
        for p in catalog.iter_products(fq, label=label):
            pid = str(p.get("productId") or "")
            if pid and pid not in seen and (p.get("items") or [{}])[0].get("itemId"):
                seen.add(pid)
                products.append(p)
            if limit and len(seen) >= limit:
                break
        batch: List[Dict[str, Any]] = []
        for i in range(0, len(products), SIM_BATCH):
            chunk = products[i:i + SIM_BATCH]
            sims = _simulate(session, [str(p["items"][0]["itemId"]) for p in chunk], zip_code)
            for p in chunk:
                sim = sims.get(str(p["items"][0]["itemId"]))
                offer = _offer(p, sim, label) if sim else None
                if offer:
                    batch.append(offer)
            time.sleep(0.1)
        if batch:
            r = db.save(batch)
            for k in total:
                total[k] += r[k]
        print(f"[paguemenos] {str(label)[:40]:<40} {len(products):>4} listed, +{len(batch):>4} priced (total {len(seen)})")
        if limit and len(seen) >= limit:
            break
    return total


if __name__ == "__main__":
    from markets.common.runner import run_cli
    run_cli(STORE_KEY, scrape)
