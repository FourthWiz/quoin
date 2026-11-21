import requests


def test_create_order():
    requests.post("http://localhost:8000/orders", json={"total_cents": 100})
