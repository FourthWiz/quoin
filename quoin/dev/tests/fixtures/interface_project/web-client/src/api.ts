const BASE = process.env.ORDER_API ?? "http://localhost:8000";

export async function getOrder(id: string) {
  const res = await fetch(`${BASE}/orders/${id}`);
  return res.json();
}

export async function createOrder(totalCents: number) {
  const res = await fetch(`${BASE}/orders`, { method: "POST", body: JSON.stringify({ total_cents: totalCents }) });
  return res.json();
}

export async function health() {
  return fetch(`${BASE}/health`);
}
