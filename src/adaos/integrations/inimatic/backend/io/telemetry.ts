import client from 'prom-client'

export const tg_updates_total = new client.Counter({ name: 'tg_updates_total', help: 'Telegram updates', labelNames: ['type'] })
export const enqueue_total = new client.Counter({ name: 'enqueue_total', help: 'Enqueued to hub', labelNames: ['hub'] })
export const outbound_total = new client.Counter({ name: 'outbound_total', help: 'Outbound deliveries', labelNames: ['type'] })
export const retry_total = new client.Counter({ name: 'retry_total', help: 'Retries', labelNames: ['stage'] })
export const dlq_total = new client.Counter({ name: 'dlq_total', help: 'DLQ publishes', labelNames: ['stage'] })

// Root<->Hub route proxy (NATS-based) metrics.
export const route_http_requests_total = new client.Counter({
	name: 'route_http_requests_total',
	help: 'HTTP requests proxied via route.to_hub/route.to_browser',
	labelNames: ['kind'],
})
export const route_http_replies_total = new client.Counter({
	name: 'route_http_replies_total',
	help: 'HTTP proxy replies received from hubs via NATS',
	labelNames: ['kind', 'status_class'],
})
export const route_http_proxy_failed_total = new client.Counter({
	name: 'route_http_proxy_failed_total',
	help: 'HTTP proxy failures (timeouts, disconnects)',
	labelNames: ['hub'],
})
export const route_ws_client_close_total = new client.Counter({
	name: 'route_ws_client_close_total',
	help: 'Browser WS clients closed (root route proxy)',
	labelNames: ['kind', 'code'],
})

// WS->NATS proxy (hub connection to root) metrics.
export const ws_nats_proxy_conn_open_total = new client.Counter({
	name: 'ws_nats_proxy_conn_open_total',
	help: 'WS->NATS proxy connections opened',
	labelNames: [],
})
export const ws_nats_proxy_conn_close_total = new client.Counter({
	name: 'ws_nats_proxy_conn_close_total',
	help: 'WS->NATS proxy connections closed',
	labelNames: ['code'],
})
export const ws_nats_proxy_upstream_close_total = new client.Counter({
	name: 'ws_nats_proxy_upstream_close_total',
	help: 'WS->NATS proxy upstream TCP closed',
	labelNames: ['had_error'],
})

