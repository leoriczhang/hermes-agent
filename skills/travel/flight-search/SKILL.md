---
name: flight-search
description: Mock flight search across major Chinese carriers.
version: 0.1.0
author: liuyue
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [Travel, Flights, Mock, Demo]
    category: travel
prerequisites:
  commands: [python3]
---

# Flight Search Skill (Mock)

Search domestic flights between two Chinese cities and return ranked options
(price, departure time, airline, duration). This skill is a deterministic
mock — it does NOT hit any real airline API. Use it as a stand-in while
you wire up a production booking integration.

## When to Use

- User asks: "查一下北京到上海明天的机票" / "上海飞成都最便宜的航班" /
  "find flights from PEK to SHA"
- Needs flight options BEFORE confirming the trip — pricing comparison,
  earliest departure, preferred airline.
- Demo / fixture data; never recommend booking based on these results.

## Prerequisites

- `python3` available on PATH.
- Cities supported: 北京/PEK, 上海/SHA, 广州/CAN, 深圳/SZX, 成都/CTU,
  杭州/HGH, 西安/XIY, 重庆/CKG. Pass either Chinese name or IATA code.

## How to Run

Use `terminal` to invoke the helper script:

```bash
python3 scripts/search_flights.py --from 北京 --to 上海 --date 2026-06-01
# or with IATA codes:
python3 scripts/search_flights.py --from PEK --to SHA --date 2026-06-01 --max-results 5
```

The script emits JSON to stdout:

```json
{
  "from": "PEK",
  "to": "SHA",
  "date": "2026-06-01",
  "results": [
    {"flight_no": "CA1501", "airline": "中国国际航空", "depart": "07:00",
     "arrive": "09:15", "duration_min": 135, "price_cny": 980, "stops": 0},
    ...
  ]
}
```

## Quick Reference

| Argument | Required | Default | Notes |
|---|---|---|---|
| `--from` | yes | — | City name or IATA |
| `--to`   | yes | — | City name or IATA |
| `--date` | yes | — | `YYYY-MM-DD` |
| `--max-results` | no | 8 | Cap output rows |
| `--sort` | no | price | `price` \| `depart` \| `duration` |

## Procedure

1. Parse user intent — origin, destination, date, sorting preference.
2. Run the script; surface top 3 results to the user as a markdown table.
3. If the user picks one, confirm it and ask whether to proceed to booking
   (out of scope for this skill — hand off to a real booking skill).

## Pitfalls

- The mock data is deterministic per (from, to, date) tuple. Same query
  same date will always return the same results — useful for testing,
  but DO NOT present it as live availability to end users.
- Prices are in CNY only.
- Same-city queries return `[]`.

## Verification

```bash
python3 scripts/search_flights.py --from 北京 --to 上海 --date 2026-06-01 \
  | python3 -c "import sys, json; d=json.load(sys.stdin); assert d['results']; print('ok')"
```
