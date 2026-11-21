Fixture project for the interface graph: three repositories under a non-git
parent. Tests copy it and run `git init` in each repository folder.

Planted pairs (provider -> consumer):
- package acme-schema: shared-schema -> order-service
- schema order.schema.json: shared-schema -> web-client
- HTTP /orders: order-service -> web-client
- event order.created: order-service -> web-client

Decoys that must not produce a pair: web-client calls /health (no provider),
order-service imports requests (not a project package), shared-schema's README
mentions order.created (docs are not scanned), order-service publishes
order.audited (no subscriber), and order-service's tests call /orders (tests
are not consumers).
