// src\adaos\integrations\inimatic\src\app\skills\hub-ping.skill.ts
import { SkillModule } from '../skill-host/skill-ctx';
import { firstValueFrom } from 'rxjs';

export const hubPingSkill: SkillModule = {
	manifest: { name: 'hub-ping', version: '0.1.3', route: '/hub/ping', capabilities: ['ui', 'net', 'io'] },
	async mount(ctx) {
		const { ui, net, io, log } = ctx;
		ui.render({
			type: 'stack', gap: 8, padding: 8, children: [
				{ type: 'text', id: 'h1', text: 'hub ping (subnet)' },
				{ type: 'button', id: 'btn', label: 'ping all' },
				{ type: 'console', id: 'log', height: 220 }
			]
		});

		ui.on('click', 'btn', async () => {
			log('click ping');
			const out = io.to('text', { target: '#log' });
			try {
				// 1) тянем обёртку и достаём nodes
				const resp = await firstValueFrom(net.adaos.get<{ ok: boolean; nodes: any[] }>('/api/subnet/nodes'));
				const nodes = resp?.nodes ?? [];
				await out.write(`nodes: ${nodes.length}`);

				if (!nodes.length) { await out.write('нет узлов'); return; }

				// 2) пингуем по node_id (фолбэк на id/name, если вдруг форма другая)
				for (const n of nodes) {
					const nodeId = n.node_id ?? n.id ?? n.name;
					if (!nodeId) {
						await out.write(`skip: malformed node ${JSON.stringify(n)}`);
						continue;
					}
					const r = await firstValueFrom(
						net.adaos.post<{ latency?: number; ok?: boolean; error?: string }>(
							'/api/subnet/ping',
							{ node_id: nodeId }             // <— ключ важен
						)
					);
					const label = n.meta?.hostname ?? n.name ?? nodeId;
					const latency = (r?.latency ?? '—');
					await out.write(`${label}: ${latency} ms`);
				}
			} catch (e: any) {
				log('[hub-ping] error', e);
				await out.write(`error: ${e?.message || e}`);
			}
		});
	}
};
