// Интерфейсы
import express, { type Express, type Request } from 'express'
import type http from 'http'
import { createProxyMiddleware } from 'http-proxy-middleware'
import { parse } from 'node:url'

const DEFAULT_BASE = process.env['ADAOS_BASE'] ?? 'http://127.0.0.1:8777'
const DEFAULT_TOKEN = process.env['ADAOS_TOKEN'] ?? 'dev-local-token'
const ADAOS_PROXY_ENABLED = process.env['ADAOS_PROXY_ENABLED'] !== '0'
const ADAOS_PROXY_UPGRADE_PREFIXES = (process.env['ADAOS_PROXY_UPGRADE_PREFIXES'] ?? '/adaos')
        .split(',')
        .map((prefix) => prefix.trim())
        .filter((prefix) => prefix.length > 0)

const resolveToken = (req: Request) => (req.header('X-AdaOS-Token') ?? DEFAULT_TOKEN) as string
const resolveBase = (req: Request) => (req.header('X-AdaOS-Base') ?? DEFAULT_BASE) as string

export function installAdaosBridge(app: Express, server: http.Server) {
        // body parser
        app.use(express.json())

        if (ADAOS_PROXY_ENABLED) {
                // /adaos/** → прокси на ноду AdaOS (HTTP+WS)
                const adaosProxy = createProxyMiddleware({
                        target: DEFAULT_BASE,
                        changeOrigin: true,
                        ws: true,
                        pathRewrite: { '^/adaos': '' },
                        router: (req) => resolveBase(req as Request),
                        on: {
                                proxyReq: (proxyReq, req) => {
                                        proxyReq.setHeader('X-AdaOS-Token', resolveToken(req as Request))
                                },
                        },
                })

                app.use('/adaos', adaosProxy)
                if (ADAOS_PROXY_UPGRADE_PREFIXES.length > 0) {
                        server.on('upgrade', (req, socket, head) => {
                                const { pathname = '' } = parse(req.url ?? '')
                                const shouldProxy = ADAOS_PROXY_UPGRADE_PREFIXES.some((prefix) =>
                                        pathname.startsWith(prefix),
                                )
                                if (!shouldProxy) {
                                        return
                                }
                                // @ts-ignore: у middleware есть upgrade
                                adaosProxy.upgrade?.(req, socket, head)
                        })
                }

                // «короткие» HUB-ручки
                app.get('/api/subnet/nodes', async (req, res) => {
                        try {
                                const r = await fetch(`${resolveBase(req)}/api/subnet/nodes`, {
                                        headers: { 'X-AdaOS-Token': resolveToken(req) },
                                })
                                if (!r.ok) throw new Error(String(r.status))
                                res.json(await r.json())
                        } catch (e: any) {
                                res.status(502).json({
                                        error: 'adaos upstream failed',
                                        detail: String(e?.message ?? e),
                                })
                        }
                })

                app.post('/api/subnet/ping', async (req, res) => {
                        try {
                                const r = await fetch(`${resolveBase(req)}/api/subnet/ping`, {
                                        method: 'POST',
                                        headers: {
                                                'content-type': 'application/json',
                                                'X-AdaOS-Token': resolveToken(req),
                                        },
                                        body: JSON.stringify(req.body ?? {}),
                                })
                                if (!r.ok) throw new Error(String(r.status))
                                res.json(await r.json())
                        } catch (e: any) {
                                res.status(502).json({
                                        error: 'adaos upstream failed',
                                        detail: String(e?.message ?? e),
                                })
                        }
                })
        }

        // health
        app.get('/healthz', (_req, res) =>
                res.json({ ok: true, adaos: DEFAULT_BASE, time: new Date().toISOString() })
        )
}

