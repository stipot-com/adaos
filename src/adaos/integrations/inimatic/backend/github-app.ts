// ESM/TypeScript
import { SignJWT, importPKCS8 } from 'jose';

export type GhAppConfig = {
	appId: string;              // GH_APP_ID
	installationId: string;     // GH_APP_INSTALLATION_ID
	privateKeyPem: string;      // содержимое PEM
};

type CachedToken = { token: string; exp: number };
let cache: CachedToken | null = null;

export async function getInstallationToken(cfg: GhAppConfig): Promise<string> {
	const now = Math.floor(Date.now() / 1000);
	if (cache && cache.exp - 120 > now) return cache.token;

	const jwt = await createAppJwt(cfg.appId, cfg.privateKeyPem);
	const url = `https://api.github.com/app/installations/${cfg.installationId}/access_tokens`;

	const res = await fetch(url, {
		method: 'POST',
		headers: {
			'Authorization': `Bearer ${jwt}`,
			'Accept': 'application/vnd.github+json',
			'User-Agent': 'inimatic-backend',
		},
	});

	if (!res.ok) {
		const txt = await res.text().catch(() => '');
		throw new Error(`GitHub token request failed: ${res.status} ${res.statusText} ${txt}`);
	}

	const data = await res.json() as { token: string; expires_at: string };
	const exp = Math.floor(new Date(data.expires_at).getTime() / 1000);
	cache = { token: data.token, exp };
	return data.token;
}

async function createAppJwt(appId: string, privateKeyPem: string): Promise<string> {
	const iat = Math.floor(Date.now() / 1000) - 60;   // время немного в прошлое
	const exp = iat + 9 * 60;                         // < 10 минут
	const key = await importPKCS8(privateKeyPem, 'RS256');

	return await new SignJWT({ iat, exp, iss: appId })
		.setProtectedHeader({ alg: 'RS256' })
		.sign(key);
}
