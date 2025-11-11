import { Component, Input } from '@angular/core'
import { IonicModule, ModalController } from '@ionic/angular'
import { CommonModule } from '@angular/common'

@Component({
  selector: 'ada-weather-modal',
  standalone: true,
  imports: [IonicModule, CommonModule],
  templateUrl: './weather-modal.component.html'
})
export class WeatherModalComponent {
  @Input() title = 'Погода'
  @Input() weather?: { city:string; temp_c:number; condition:string; wind_ms:number; updated_at:string }
  constructor(private modal: ModalController) {}
  close(){ this.modal.dismiss() }
}

