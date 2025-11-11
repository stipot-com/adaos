// src\adaos\integrations\inimatic\src\app\skill-host\skill-outlet.component.ts
import { Component, ElementRef, Input, OnDestroy, AfterViewInit } from '@angular/core';
import { SkillHostService } from './skill-host.service';

@Component({
	selector: 'skill-outlet',
	template: `<div class="skill-outlet" style="display:block;width:100%;height:100%"></div>`,
	standalone: true
})
export class SkillOutletComponent implements AfterViewInit, OnDestroy {
	@Input() skill!: string;
	private unmount?: () => void;

	constructor(private el: ElementRef<HTMLElement>, private host: SkillHostService) { }
	async ngAfterViewInit() { this.unmount = await this.host.mount(this.skill, this.el); }
	ngOnDestroy() { try { this.unmount?.(); } catch { } }
}
