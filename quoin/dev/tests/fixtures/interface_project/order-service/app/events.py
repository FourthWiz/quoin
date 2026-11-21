import requests


def publish(topic: str, payload: dict) -> None:
    requests.post("https://bus.internal/publish", json={"topic": topic, "payload": payload})
