//src\adaos\integrations\inimatic\src\app\skill-host\skill-ctx.ts
import { AdaosClient } from '../core/adaos/adaos-client.service';

export type Ability = 'text' | 'voice' | 'api' | 'ws';
export type IOSettings = { input: { abilities: Ability[], active: Ability }, output: { abilities: Ability[], active: Ability } };

export interface UILayout { type: string; id?: string;[k: string]: any }
export interface SkillManifest { name: string; version: string; route?: string; capabilities?: string[] }

export interface SkillModule {
	manifest: SkillManifest;
	mount(ctx: SkillCtx): Promise<void> | void;
	unmount?(ctx: SkillCtx): void;
}

export interface SkillCtx {
	el: HTMLElement;
	ui: {
		render(tree: UILayout): void;
		patch(id: string, props: Record<string, any>): void;
		on(evt: string, id: string, cb: (e: any) => void): void;
		unmount(): void;
	};
	io: {
		settings(): IOSettings;
		set(x: Partial<IOSettings>): void;
		to(kind: Ability, spec?: any): { write(msg: any): Promise<void> };
	};
	net: { adaos: AdaosClient };
	bus: { send(topic: string, payload: any): void };
	state: Map<string, any>;
	log: (...args: any[]) => void;
}
