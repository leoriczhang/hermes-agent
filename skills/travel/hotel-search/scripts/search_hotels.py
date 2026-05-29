#!/usr/bin/env python3
"""Mock hotel search — deterministic fake data for demo / fixture use."""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
from datetime import datetime

CITY_TO_IATA = {
    "北京": "PEK", "上海": "SHA", "广州": "CAN", "深圳": "SZX",
    "成都": "CTU", "杭州": "HGH", "西安": "XIY", "重庆": "CKG",
    "PEK": "PEK", "SHA": "SHA", "CAN": "CAN", "SZX": "SZX",
    "CTU": "CTU", "HGH": "HGH", "XIY": "XIY", "CKG": "CKG",
}
IATA_TO_CITY = {v: k for k, v in CITY_TO_IATA.items() if not k.isupper()}

DISTRICTS = {
    "PEK": ["朝阳区", "海淀区", "东城区", "西城区"],
    "SHA": ["黄浦区", "静安区", "徐汇区", "浦东新区"],
    "CAN": ["天河区", "越秀区", "海珠区"],
    "SZX": ["福田区", "南山区", "罗湖区"],
    "CTU": ["锦江区", "青羊区", "武侯区"],
    "HGH": ["西湖区", "上城区", "下城区"],
    "XIY": ["碑林区", "雁塔区", "莲湖区"],
    "CKG": ["渝中区", "江北区", "渝北区"],
}
BRAND_TEMPLATES = [
    "{city}{loc}{brand}酒店",
    "{brand}·{city}{loc}店",
    "{city}{loc}{brand}大酒店",
]
BRANDS = ["茂悦", "希尔顿", "万豪", "凯悦", "洲际", "丽思", "君悦",
          "如家", "汉庭", "桔子", "全季", "亚朵", "华住", "锦江"]
LOCATIONS = ["外滩", "市中心", "商务区", "古城", "高铁站", "机场", "湖畔"]
AMENITIES_POOL = ["wifi", "breakfast", "gym", "pool", "spa",
                  "parking", "restaurant", "bar", "shuttle"]


def _seeded_rng(*parts: str) -> random.Random:
    h = hashlib.sha256("|".join(parts).encode()).hexdigest()
    return random.Random(int(h[:16], 16))


def _resolve(code: str) -> str:
    iata = CITY_TO_IATA.get(code) or CITY_TO_IATA.get(code.upper())
    if not iata:
        raise SystemExit(f"unknown city: {code}")
    return iata


def search(city: str, checkin: str, checkout: str,
           max_price: int | None, min_stars: int,
           max_results: int, sort_by: str):
    iata = _resolve(city)
    city_cn = IATA_TO_CITY[iata]
    try:
        d_in = datetime.strptime(checkin, "%Y-%m-%d")
        d_out = datetime.strptime(checkout, "%Y-%m-%d")
    except ValueError:
        raise SystemExit("invalid date (YYYY-MM-DD expected)")
    nights = (d_out - d_in).days
    if nights <= 0:
        raise SystemExit("checkout must be after checkin")

    rng = _seeded_rng(iata, checkin, checkout)
    n = rng.randint(10, 18)
    results = []
    for _ in range(n):
        stars = rng.choices([3, 4, 5], weights=[3, 4, 3])[0]
        base_price = {3: 380, 4: 680, 5: 1280}[stars]
        price = base_price + rng.randint(-120, 600)
        rating = round(min(5.0, 3.5 + (stars - 3) * 0.3 + rng.random() * 0.6), 1)
        district = rng.choice(DISTRICTS.get(iata, ["市中心"]))
        brand = rng.choice(BRANDS)
        loc = rng.choice(LOCATIONS)
        name = rng.choice(BRAND_TEMPLATES).format(city=city_cn, loc=loc, brand=brand)
        amenities = rng.sample(AMENITIES_POOL, k=rng.randint(2, 5))

        if stars < min_stars:
            continue
        if max_price is not None and price > max_price:
            continue

        results.append({
            "name": name,
            "stars": stars,
            "rating": rating,
            "price_per_night_cny": price,
            "total_cny": price * nights,
            "district": district,
            "amenities": amenities,
        })

    sort_keys = {
        "price": lambda r: r["price_per_night_cny"],
        "rating": lambda r: -r["rating"],
        "stars": lambda r: -r["stars"],
    }
    results.sort(key=sort_keys.get(sort_by, sort_keys["rating"]))
    results = results[:max_results]

    return {
        "city": city_cn, "iata": iata,
        "checkin": checkin, "checkout": checkout,
        "nights": nights,
        "filter": {"max_price": max_price, "min_stars": min_stars},
        "sort_by": sort_by,
        "results": results,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Mock hotel search.")
    ap.add_argument("--city", required=True)
    ap.add_argument("--checkin", required=True, help="YYYY-MM-DD")
    ap.add_argument("--checkout", required=True, help="YYYY-MM-DD")
    ap.add_argument("--max-price", type=int, default=None)
    ap.add_argument("--min-stars", type=int, default=0)
    ap.add_argument("--max-results", type=int, default=8)
    ap.add_argument("--sort", default="rating", choices=["price", "rating", "stars"])
    args = ap.parse_args()
    out = search(args.city, args.checkin, args.checkout,
                 args.max_price, args.min_stars, args.max_results, args.sort)
    json.dump(out, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
