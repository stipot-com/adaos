import { ComponentFixture, TestBed, waitForAsync } from '@angular/core/testing'
import { IonicModule } from '@ionic/angular'

import { PublicPointComponent } from './public-point.component'

describe('DistributionComponent', () => {
	let component: PublicPointComponent
	let fixture: ComponentFixture<PublicPointComponent>

	beforeEach(waitForAsync(() => {
		TestBed.configureTestingModule({
			declarations: [PublicPointComponent],
			imports: [IonicModule.forRoot()],
		}).compileComponents()

		fixture = TestBed.createComponent(PublicPointComponent)
		component = fixture.componentInstance
		fixture.detectChanges()
	}))

	it('should create', () => {
		expect(component).toBeTruthy()
	})
})
