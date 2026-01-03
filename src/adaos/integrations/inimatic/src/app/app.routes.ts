import { Routes } from '@angular/router'
import { HubComponent } from './features/hub/hub.component'
import { DesktopRendererComponent } from './renderer/desktop/desktop.component'

export const routes: Routes = [
	{
		path: 'desktop2',
		component: DesktopRendererComponent,
	},
	{
		path: 'private',
		loadComponent: () =>
			import('./private-point/private-point.page').then(
				(m) => m.PrivatePointPage
			),
	},
	{
		path: 'public',
		loadComponent: () =>
			import('./public-point/public-point.component').then(
				(m) => m.PublicPointComponent
			),
	},
	{
		path: 'follower',
		loadComponent: () =>
			import('./phone/phone.component').then((m) => m.PhoneComponent),
	},
	{
		path: 'hub',
		loadComponent: () =>
			import('./features/hub/hub.component').then((m) => m.HubComponent),
	},
	// legacy /member route: keep as redirect to declarative desktop2
	{ path: 'member', redirectTo: '/desktop2', pathMatch: 'full' },
	{
		path: 'desktop',
		loadComponent: () =>
			import('./desktop/desktop.component').then(
				(m) => m.DesktopComponent
			),
	},
	{
		path: '',
		redirectTo: '/desktop2',
		pathMatch: 'full',
	},
	{ path: '**', redirectTo: 'hub' }, // wildcard — в самый конец!
]
