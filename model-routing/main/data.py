"""The shop's data: a few orders and a small product catalog. Replace with your own (README, "Adapt it")."""

from __future__ import annotations

ORDERS = {
    1041: {"status": "delivered", "items": 1, "total_cad": 18.50},
    1042: {"status": "shipped", "items": 2, "total_cad": 54.00},
    1043: {"status": "packing", "items": 3, "total_cad": 96.25},
    1044: {"status": "cancelled", "items": 1, "total_cad": 22.00},
    1045: {"status": "delivered", "items": 4, "total_cad": 131.75},
}

CATALOG = [
    {"id": "p01", "name": "Trail running shoe", "kind": "shoes", "price_cad": 139.00, "tags": "running trail grip waterproof"},
    {"id": "p02", "name": "Road running shoe", "kind": "shoes", "price_cad": 119.00, "tags": "running road light"},
    {"id": "p03", "name": "Hiking boot", "kind": "shoes", "price_cad": 189.00, "tags": "hiking waterproof ankle"},
    {"id": "p04", "name": "Rain jacket", "kind": "jacket", "price_cad": 149.00, "tags": "rain waterproof hood light"},
    {"id": "p05", "name": "Insulated jacket", "kind": "jacket", "price_cad": 229.00, "tags": "winter warm down"},
    {"id": "p06", "name": "Wool socks, 3 pairs", "kind": "socks", "price_cad": 24.00, "tags": "wool warm hiking"},
    {"id": "p07", "name": "Running socks, 3 pairs", "kind": "socks", "price_cad": 18.00, "tags": "running light"},
    {"id": "p08", "name": "Day pack 20 L", "kind": "bag", "price_cad": 89.00, "tags": "hiking pack light"},
    {"id": "p09", "name": "Water bottle 750 mL", "kind": "bottle", "price_cad": 22.00, "tags": "bottle steel"},
    {"id": "p10", "name": "Headlamp", "kind": "light", "price_cad": 45.00, "tags": "light night running hiking"},
    {"id": "p11", "name": "Trekking poles", "kind": "poles", "price_cad": 99.00, "tags": "hiking poles carbon"},
    {"id": "p12", "name": "Merino base layer", "kind": "shirt", "price_cad": 79.00, "tags": "wool warm winter"},
]

POLICY = "Returns within 30 days of delivery, unused, with the order number. Shipping is free over 75 CAD."
