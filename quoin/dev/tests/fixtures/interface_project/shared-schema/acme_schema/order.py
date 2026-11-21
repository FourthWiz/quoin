from dataclasses import dataclass


@dataclass
class Order:
    order_id: str
    total_cents: int
