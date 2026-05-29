---
name: hotel-search
description: Mock hotel search by city, date range, and budget.
version: 0.1.0
author: liuyue
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [Travel, Hotels, Mock, Demo]
    category: travel
prerequisites:
  commands: [python3]
---

# Hotel Search Skill (Mock)

Search hotels in a Chinese city for a given check-in/check-out window and
budget. Like the flight skill, this is deterministic mock data — never
present results as real availability.

## When to Use

- User asks: "上海 6/1 到 6/3 的酒店推荐" / "深圳市中心 800 以内的酒店" /
  "find hotels in Hangzhou near West Lake under 1000 CNY"
- Pair with `flight-search` for trip planning demos.

## Prerequisites

- `python3` available on PATH.
- Cities supported (same list as flight-search):
  北京/PEK, 上海/SHA, 广州/CAN, 深圳/SZX, 成都/CTU, 杭州/HGH, 西安/XIY, 重庆/CKG.

## How to Run

```bash
python3 scripts/search_hotels.py \
  --city 上海 --checkin 2026-06-01 --checkout 2026-06-03 \
  --max-price 1000 --max-results 5
```

Output (JSON):

```json
{
  "city": "上海",
  "checkin": "2026-06-01",
  "checkout": "2026-06-03",
  "nights": 2,
  "results": [
    {"name": "上海外滩茂悦大酒店", "stars": 5, "rating": 4.7,
     "price_per_night_cny": 1280, "total_cny": 2560,
     "district": "黄浦区", "amenities": ["wifi", "breakfast", "gym"]},
    ...
  ]
}
```

## Quick Reference

| Argument | Required | Default | Notes |
|---|---|---|---|
| `--city` | yes | — | Chinese name or IATA |
| `--checkin`  | yes | — | `YYYY-MM-DD` |
| `--checkout` | yes | — | `YYYY-MM-DD`, must be > checkin |
| `--max-price` | no | unlimited | Per-night cap in CNY |
| `--min-stars` | no | 0 | 1–5 |
| `--max-results` | no | 8 | Cap output rows |
| `--sort` | no | rating | `price` \| `rating` \| `stars` |

## Procedure

1. Parse intent — city, dates, budget, must-haves.
2. Run script; render top 3 as a markdown table.
3. If user picks one, do NOT pretend to book — that requires a real
   integration; hand off explicitly.

## Pitfalls

- Deterministic per (city, checkin, checkout). Don't rely on it for real
  pricing.
- Total price = `price_per_night_cny × nights` (no taxes / fees modeled).
- `checkout <= checkin` returns an error.

## Verification

```bash
python3 scripts/search_hotels.py --city 上海 \
  --checkin 2026-06-01 --checkout 2026-06-03 \
  | python3 -c "import sys, json; d=json.load(sys.stdin); assert d['results']; print('ok')"
```
