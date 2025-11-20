import { ComponentFixture, TestBed } from '@angular/core/testing'

import { PrivatePointPage } from './private-point.page'

describe('PrivatePointPage', () => {
	let component: PrivatePointPage
	let fixture: ComponentFixture<PrivatePointPage>

	beforeEach(async () => {
		fixture = TestBed.createComponent(PrivatePointPage)
		component = fixture.componentInstance
		fixture.detectChanges()
	})

	it('should create', () => {
		expect(component).toBeTruthy()
	})
})
