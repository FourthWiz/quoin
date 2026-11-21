import { subscribe } from "./bus";

subscribe("order.created", (event: { order_id: string }) => {
  console.log("new order", event.order_id);
});
