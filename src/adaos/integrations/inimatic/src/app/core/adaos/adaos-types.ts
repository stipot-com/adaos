export type AdaosEvent = { type: string;[k: string]: any };

export interface AdaosConfig {
	baseUrl: string;        // напр. 'http://127.0.0.1:8777' или '/adaos' (через прокси)
	token?: string | null;  // dev-local-token
}
