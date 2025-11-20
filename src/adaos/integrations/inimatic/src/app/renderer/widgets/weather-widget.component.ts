import { Component, Input } from '@angular/core'
import { CommonModule } from '@angular/common'
import { IonicModule } from '@ionic/angular'

@Component({
  selector: 'ada-weather-widget',
  standalone: true,
  imports: [CommonModule, IonicModule],
  templateUrl: './weather-widget.component.html'
})
export class WeatherWidgetComponent {
  @Input() title = 'Погода'
  @Input() data?: { city: string; temp_c: number; condition: string; wind_ms: number; updated_at: string }
}

