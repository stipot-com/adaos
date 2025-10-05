import { HttpClient, HttpHeaders } from '@angular/common/http';
import { Injectable } from '@angular/core';

export type AdaosEvent = { type: string;[k: string]: any };
export interface AdaosConfig { baseUrl: string; token?: string | null; }

export interface SubnetRegisterRequest {
        csr_pem: string;
        fingerprint: string;
        owner_token: string;
        hints?: any;
        idempotencyKey?: string;
}

export interface SubnetRegisterData {
        subnet_id: string;
        hub_device_id: string;
        cert_pem: string;
}

export interface SubnetRegisterResponse {
        data: SubnetRegisterData | null;
        event_id: string;
        server_time_utc: string;
}

const ROOT_BASE = (() => {
        const value = (window as any).__ADAOS_ROOT_BASE__ ?? 'http://127.0.0.1:3030';
        return typeof value === 'string' ? value.replace(/\/$/, '') : 'http://127.0.0.1:3030';
})();

function rootAbs(path: string) {
        const rel = path.startsWith('/') ? path : `/${path}`;
        return `${ROOT_BASE}${rel}`;
}

@Injectable({ providedIn: 'root' })
export class AdaosClient {
	private ws?: WebSocket;
	private cfg: AdaosConfig;

	constructor(private http: HttpClient) {
		this.cfg = {
			baseUrl: (window as any).__ADAOS_BASE__ ?? 'http://127.0.0.1:8777',
			token: (window as any).__ADAOS_TOKEN__ ?? null,
		};
	}

	setBase(url: string) { this.cfg.baseUrl = url.replace(/\/$/, ''); }
	setToken(token: string | null) { this.cfg.token = token; }

	// аккуратная склейка без new URL — работает и с абсолютной, и с относительной базой
	private abs(path: string) {
		const base = this.cfg.baseUrl.replace(/\/$/, '');
		const rel = path.startsWith('/') ? path : `/${path}`;
		return `${base}${rel}`;
	}
	private h() {
		return this.cfg.token ? new HttpHeaders({ 'X-AdaOS-Token': this.cfg.token }) : undefined;
	}

	get<T>(path: string) { return this.http.get<T>(this.abs(path), { headers: this.h() }); }
	post<T>(path: string, body?: any) { return this.http.post<T>(this.abs(path), body ?? {}, { headers: this.h() }); }

	// WebSocket напрямую к локальной ноде
	connect(topics: string[] = []) {
		const wsUrl = this.abs('/ws').replace(/^http/, 'ws');
		const u = new URL(wsUrl);
		if (this.cfg.token) u.searchParams.set('token', this.cfg.token);
		this.ws = new WebSocket(u.toString());
		this.ws.onopen = () => { if (topics.length) this.subscribe(topics); };
		return this.ws;
	}
	subscribe(topics: string[]) { this.ws?.send(JSON.stringify({ type: 'subscribe', topics })); }

	say(text: string) { return this.post('/api/say', { text }); }
	callSkill<T = any>(skill: string, method: string, body?: any) {
		return this.post<T>(`/api/skills/${skill}/${method}`, body ?? {});
	}
}

export async function subnetRegister(req: SubnetRegisterRequest): Promise<SubnetRegisterResponse> {
        const headers: Record<string, string> = { 'Content-Type': 'application/json' };
        if (req.idempotencyKey) headers['Idempotency-Key'] = req.idempotencyKey;
        const response = await fetch(rootAbs('/v1/subnets/register'), {
                method: 'POST',
                headers,
                body: JSON.stringify({
                        csr_pem: req.csr_pem,
                        fingerprint: req.fingerprint,
                        owner_token: req.owner_token,
                        hints: req.hints ?? null,
                }),
        });
        if (!response.ok) throw new Error(`subnetRegister failed: ${response.status}`);
        return response.json();
}

export async function subnetRegisterStatus(fingerprint: string, ownerToken?: string): Promise<SubnetRegisterResponse> {
        const token = ownerToken ?? (window as any).__ADAOS_ROOT_OWNER_TOKEN__;
        if (!token) throw new Error('owner token required');
        const response = await fetch(rootAbs(`/v1/subnets/register/status?fingerprint=${encodeURIComponent(fingerprint)}`), {
                headers: { 'X-Owner-Token': token },
        });
        if (!response.ok) throw new Error(`subnetRegisterStatus failed: ${response.status}`);
        return response.json();
}
