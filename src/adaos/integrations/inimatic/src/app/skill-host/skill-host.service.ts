// src\adaos\integrations\inimatic\src\app\skill-host\skill-host.service.ts
import { Injectable, ElementRef } from '@angular/core';
import { AdaosClient } from '../core/adaos/adaos-client.service';
import { SkillCtx, SkillModule, IOSettings } from './skill-ctx';
import { renderInto, patch } from './ui-lib';
import { isDebugEnabled } from '../debug-log';

@Injectable({ providedIn: 'root' })
export class SkillHostService {
	private regs = new Map<string, SkillModule>();

	constructor(private adaos: AdaosClient) { }

	register(mod: SkillModule) { this.regs.set(mod.manifest.name, mod); }
	list() { return [...this.regs.keys()]; }

	async mount(name: string, elRef: ElementRef<HTMLElement>) {
		const mod = this.regs.get(name); if (!mod) throw new Error('skill not found: ' + name);
		const el = elRef.nativeElement; const state = new Map<string, any>();
		const ioSettings: IOSettings = { input: { abilities: ['text', 'voice', 'api', 'ws'], active: 'text' }, output: { abilities: ['text', 'voice', 'api'], active: 'text' } };

		const ctx: SkillCtx = {
			el,
			ui: {
				render: (tree) => renderInto(el, tree),
				patch: (id, props) => patch(el, id, props),
				on: (evt, id, cb) => { el.addEventListener(evt, (e: any) => { if ((e.target as HTMLElement)?.id === id) cb(e); }); },
				unmount: () => el.replaceChildren()
			},
			io: {
				settings: () => ioSettings,
				set: (x) => Object.assign(ioSettings.input, x.input || {}, ioSettings.output, x.output || {}),
				to: (kind, spec) => ({
					write: async (msg: any) => {
						if (kind === 'text') {
							const target = spec?.target ? el.querySelector(spec.target) as HTMLElement : el;
							const s = typeof msg === 'string' ? msg : JSON.stringify(msg);
							const pre = target?.tagName === 'PRE' ? target : (target?.querySelector('pre') as HTMLElement);
							(pre || target).append?.(document.createTextNode((s + '\n')));
						} else if (kind === 'voice') {
							await this.adaos.say(typeof msg === 'string' ? msg : JSON.stringify(msg)).toPromise();
						} else if (kind === 'api') {
							await fetch(spec?.url, { method: spec?.method || 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(msg) });
						}
					}
				})
			},
			net: { adaos: this.adaos },
			bus: { send: (topic, payload) => this.adaos.post('/api/bus/publish', { topic, payload }).subscribe() },
			state,
			log: (...a: any[]) => { if (isDebugEnabled()) console.log('[skill]', name, ...a); },
		};

		await mod.mount(ctx);
		return () => mod.unmount?.(ctx);
	}
}
