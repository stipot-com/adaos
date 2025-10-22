import client from 'prom-client'

export const tg_updates_total = new client.Counter({ name: 'tg_updates_total', help: 'Telegram updates', labelNames: ['type'] })
export const enqueue_total = new client.Counter({ name: 'enqueue_total', help: 'Enqueued to hub', labelNames: ['hub'] })
export const outbound_total = new client.Counter({ name: 'outbound_total', help: 'Outbound deliveries', labelNames: ['type'] })
export const retry_total = new client.Counter({ name: 'retry_total', help: 'Retries', labelNames: ['stage'] })
export const dlq_total = new client.Counter({ name: 'dlq_total', help: 'DLQ publishes', labelNames: ['stage'] })

