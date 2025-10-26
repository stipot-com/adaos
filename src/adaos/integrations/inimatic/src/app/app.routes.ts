import { Routes } from '@angular/router'
import { HubComponent } from './features/hub/hub.component'
import { MemberComponent } from './features/member/member.component'

export const routes: Routes = [
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
	{ path: 'member', component: MemberComponent },
	{
		path: 'desktop',
		loadComponent: () =>
			import('./desktop/desktop.component').then(
				(m) => m.DesktopComponent
			),
	},
	{
		path: '',
		redirectTo: '/private',
		pathMatch: 'full',
	},
	{ path: '**', redirectTo: 'hub' }, // wildcard — в самый конец!
]
