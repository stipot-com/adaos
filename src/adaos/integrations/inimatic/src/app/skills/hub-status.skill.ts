// src\adaos\integrations\inimatic\src\app\skills\hub-status.skill.ts
import { SkillModule } from '../skill-host/skill-ctx';
import { firstValueFrom } from 'rxjs';

type SubnetNode = {
	node_id: string;
	last_seen: number; // сек с плавающей запятой
	status?: string;   // например, 'up'
	meta?: { hostname?: string; roles?: string[] };
};

type SubnetResp = { ok: boolean; nodes: SubnetNode[] };
type LocalStatus = {
	node_id: string; subnet_id?: string;
	role: 'hub' | 'member' | string; hub_url?: string;
	ready?: boolean;
};

function humanAgeSec(lastSeenSec: number): string {
	const nowSec = Date.now() / 1000;
	const age = Math.max(0, Math.round(nowSec - lastSeenSec));
	if (age < 5) return 'now';
	if (age < 60) return `${age}s`;
	const m = Math.floor(age / 60);
	const s = age % 60;
	return `${m}m ${s}s`;
}

export const hubStatusSkill: SkillModule = {
	manifest: { name: 'hub-status', version: '0.2.0', route: '/hub/status', capabilities: ['ui', 'net', 'io'] },
	async mount(ctx) {
		const { ui, net, io, log } = ctx;
		ui.render({
			type: 'stack', gap: 8, padding: 8, children: [
				{ type: 'text', id: 'h1', text: 'subnet status' },
				{ type: 'button', id: 'btn', label: 'refresh' },
				{ type: 'console', id: 'log', height: 280 }
			]
		});

		const out = io.to('text', { target: '#log' });

		async function load() {
			try {
				// локальная нода (для уточнения роли/готовности)
				let local: LocalStatus | null = null;
				try {
					local = await firstValueFrom(net.adaos.get<LocalStatus>('/api/node/status'));
				} catch (e) {
					// необязательно, может быть не hub
				}

				// список узлов в подсети (у хаба — полный список; у мембера — может быть только он сам)
				const resp = await firstValueFrom(net.adaos.get<SubnetResp>('/api/subnet/nodes'));
				const nodes = resp?.nodes ?? [];
				await out.write(`nodes: ${nodes.length}`);

				if (!nodes.length) {
					await out.write('нет узлов в подсети');
					return;
				}

				for (const n of nodes) {
					const label = n.meta?.hostname ?? n.node_id;
					const roles = (n.meta?.roles && n.meta.roles.length) ? n.meta.roles.join(',') : '—';
					const age = humanAgeSec(n.last_seen);
					const isLocal = local && local.node_id === n.node_id;

					let extra = '';
					if (isLocal && local) {
						extra = ` role=${local.role} ready=${String(local.ready)} hub=${local.hub_url ?? '—'}`;
					}
					await out.write(`${label} [${n.status ?? 'up'}] roles=${roles} seen=${age}${extra}`);
				}
			} catch (e: any) {
				log('[hub-status] error', e);
				await out.write(`error: ${e?.message || e}`);
			}
		}

		ui.on('click', 'btn', load);
		log('hub-status mounted');
		await load();
	}
};
