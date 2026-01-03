import { Routes } from '@angular/router'
import { DesktopRendererComponent } from './renderer/desktop/desktop.component'

export const routes: Routes = [
	{
		path: '',
		component: DesktopRendererComponent,
	},
	// legacy routes (keep old bookmarks/QR links working)
	{ path: 'desktop2', redirectTo: '', pathMatch: 'full' },
	{ path: 'member', redirectTo: '', pathMatch: 'full' },
	{ path: '**', redirectTo: '' },
]

