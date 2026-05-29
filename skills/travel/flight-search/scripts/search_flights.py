#!/usr/bin/env python3
"""Mock flight search — deterministic fake data for demo / fixture use."""
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
IATA_TO_CITY = {
    "PEK": "北京", "SHA": "上海", "CAN": "广州", "SZX": "深圳",
    "CTU": "成都", "HGH": "杭州", "XIY": "西安", "CKG": "重庆",
}
AIRLINES = [
    ("CA", "中国国际航空"),
    ("MU", "中国东方航空"),
    ("CZ", "中国南方航空"),
    ("HU", "海南航空"),
    ("9C", "春秋航空"),
    ("MF", "厦门航空"),
]


def _seeded_rng(*parts: str) -> random.Random:
    h = hashlib.sha256("|".join(parts).encode()).hexdigest()
    return random.Random(int(h[:16], 16))


def _resolve(code: str) -> str:
    iata = CITY_TO_IATA.get(code) or CITY_TO_IATA.get(code.upper())
    if not iata:
        raise SystemExit(f"unknown city/IATA: {code}")
    return iata


def search(origin: str, dest: str, date: str, max_results: int, sort_by: str):
    o, d = _resolve(origin), _resolve(dest)
    if o == d:
        return {"from": o, "to": d, "date": date, "results": []}
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        raise SystemExit(f"invalid date (YYYY-MM-DD expected): {date}")

    rng = _seeded_rng(o, d, date)
    n = rng.randint(6, 12)
    results = []
    for i in range(n):
        code, name = rng.choice(AIRLINES)
        flight_no = f"{code}{rng.randint(1000, 9999)}"
        depart_h = rng.randint(6, 22)
        depart_m = rng.choice([0, 5, 15, 25, 30, 45, 50])
        duration = rng.randint(95, 220)
        arr_total = depart_h * 60 + depart_m + duration
        arr_h, arr_m = (arr_total // 60) % 24, arr_total % 60
        price = rng.choice([580, 720, 850, 980, 1180, 1320, 1480, 1680, 1980, 2380])
        stops = 0 if rng.random() < 0.85 else 1
        results.append({
            "flight_no": flight_no,
            "airline": name,
            "depart": f"{depart_h:02d}:{depart_m:02d}",
            "arrive": f"{arr_h:02d}:{arr_m:02d}",
            "duration_min": duration,
            "price_cny": price,
            "stops": stops,
        })

    sort_keys = {
        "price": lambda r: r["price_cny"],
        "depart": lambda r: r["depart"],
        "duration": lambda r: r["duration_min"],
    }
    results.sort(key=sort_keys.get(sort_by, sort_keys["price"]))
    results = results[:max_results]

    return {
        "from": o, "to": d,
        "from_city": IATA_TO_CITY[o], "to_city": IATA_TO_CITY[d],
        "date": date,
        "sort_by": sort_by,
        "results": results,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Mock flight search.")
    ap.add_argument("--from", dest="origin", required=True)
    ap.add_argument("--to", dest="dest", required=True)
    ap.add_argument("--date", required=True, help="YYYY-MM-DD")
    ap.add_argument("--max-results", type=int, default=8)
    ap.add_argument("--sort", default="price", choices=["price", "depart", "duration"])
    args = ap.parse_args()
    json.dump(search(args.origin, args.dest, args.date, args.max_results, args.sort),
              sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
