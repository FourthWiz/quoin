from acme_schema import Order

from .app import app
from .events import publish


@app.get("/orders/{order_id}")
def get_order(order_id: str) -> dict:
    return {"order_id": order_id, "total_cents": 0}


@app.post("/orders")
def create_order(payload: dict) -> dict:
    order = Order(order_id="o-1", total_cents=payload["total_cents"])
    publish("order.created", {"order_id": order.order_id})
    publish("order.audited", {"order_id": order.order_id})
    return {"order_id": order.order_id}
