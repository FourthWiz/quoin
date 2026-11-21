import orderSchema from "../../shared-schema/schemas/order.schema.json";

export function validOrder(value: unknown): boolean {
  return typeof value === "object" && value !== null && orderSchema.required.every((k: string) => k in (value as object));
}
