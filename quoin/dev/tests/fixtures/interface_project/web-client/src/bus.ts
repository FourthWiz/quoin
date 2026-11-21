export function subscribe(topic: string, handler: (event: any) => void) {
  return { topic, handler };
}
