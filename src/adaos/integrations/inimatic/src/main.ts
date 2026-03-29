import { enableProdMode, isDevMode } from '@angular/core'
import { bootstrapApplication } from '@angular/platform-browser'
import { provideHttpClient, withInterceptorsFromDi } from '@angular/common/http'
import { RouteReuseStrategy, provideRouter } from '@angular/router'
import {
	IonicRouteStrategy,
	provideIonicAngular,
} from '@ionic/angular/standalone'

import { routes } from './app/app.routes'
import { AppComponent } from './app/app.component'
import { environment } from './environments/environment'
import { provideServiceWorker } from '@angular/service-worker'
import { registerIcons } from './app/icons'
import { initDebugConsole } from './app/debug-log'

if (environment.production) {
	enableProdMode()
}

const boot = (window as any).__INIMATIC_BOOT__
boot?.note?.('main.ts: module evaluated')

try {
	registerIcons()
	boot?.note?.('main.ts: icons registered')
} catch (err) {
	try {
		const message =
			(typeof (err as any)?.message === 'string' && (err as any).message) ||
			'registerIcons failed'
		boot?.fail?.('Bootstrap preparation failed', `registerIcons: ${message}`)
	} catch {}
	throw err
}

try {
	initDebugConsole()
	boot?.note?.('main.ts: debug console ready')
} catch (err) {
	try {
		const message =
			(typeof (err as any)?.message === 'string' && (err as any).message) ||
			'initDebugConsole failed'
		boot?.fail?.('Bootstrap preparation failed', `initDebugConsole: ${message}`)
	} catch {}
	throw err
}

boot?.note?.('main.ts: bootstrapApplication()')
bootstrapApplication(AppComponent, {
	providers: [
		{ provide: RouteReuseStrategy, useClass: IonicRouteStrategy },
		provideIonicAngular(),
		provideHttpClient(withInterceptorsFromDi()),
		provideRouter(routes),
		provideServiceWorker('ngsw-worker.js', {
			enabled: false,
			registrationStrategy: 'registerWhenStable:30000',
		}),
	],
})
	.then(() => {
		try {
			boot?.note?.('main.ts: bootstrap complete')
			boot?.update?.(
				'Loading Inimatic...',
				'Angular bootstrap completed. Waiting for app UI state...'
			)
		} catch {}
	})
	.catch((err) => {
		try {
			const message =
				(typeof err?.message === 'string' && err.message) ||
				(typeof err === 'string' && err) ||
				'bootstrap failed'
			boot?.note?.(`main.ts: bootstrap failed: ${message}`)
			boot?.fail?.('Angular bootstrap failed', message)
		} catch {}
		throw err
	})
