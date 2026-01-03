import { HttpClient, HttpHeaders, HttpParams } from '@angular/common/http'
import { Injectable } from '@angular/core'
import { Observable } from 'rxjs'

type PairCreateResponse = { ok: boolean; pair_code?: string; expires_at?: number }
type PairStatusResponse = {
	ok: boolean
	state?: string
	expires_in?: number
	session_jwt?: string | null
	hub_id?: string | null
	webspace_id?: string | null
	error?: string
}

@Injectable({ providedIn: 'root' })
export class PairingService {
	private readonly base: string

	private resolveBase(): string {
		try {
			const hinted = ((window as any).__ADAOS_ROOT_BASE__ || '').trim()
			if (hinted) return hinted.replace(/\/+$/, '')
		} catch {}
		// Default to current origin for local/dev deployments.
		// For production UI served from app.inimatic.com, the API lives on api.inimatic.com.
		try {
			const origin = String(window.location.origin || '').trim()
			if (origin) {
				const host = String(window.location.host || '').toLowerCase()
				if (host === 'app.inimatic.com') return 'https://api.inimatic.com'
				return origin.replace(/\/+$/, '')
			}
		} catch {}
		return 'https://api.inimatic.com'
	}

	constructor(private http: HttpClient) {
		this.base = this.resolveBase()
	}

	getBaseUrl(): string {
		return this.base
	}

	createBrowserPair(ttlSec = 600): Observable<PairCreateResponse> {
		return this.http.post<PairCreateResponse>(`${this.base}/v1/browser/pair/create`, {
			ttl: ttlSec,
		})
	}

	getBrowserPairStatus(code: string): Observable<PairStatusResponse> {
		const params = new HttpParams().set('code', code)
		return this.http.get<PairStatusResponse>(`${this.base}/v1/browser/pair/status`, {
			params,
		})
	}

	approveBrowserPair(
		code: string,
		webspaceId: string,
	): Observable<{ ok: boolean; error?: string }> {
		const jwt = (() => {
			try {
				return (localStorage.getItem('adaos_web_session_jwt') || '').trim()
			} catch {
				return ''
			}
		})()
		const headers = jwt ? new HttpHeaders({ Authorization: `Bearer ${jwt}` }) : undefined
		return this.http.post<{ ok: boolean; error?: string }>(
			`${this.base}/v1/browser/pair/approve`,
			{ code, webspace_id: webspaceId },
			{ headers },
		)
	}
}
