// src\adaos\integrations\inimatic\src\app\renderer\modals\weather-modal.component.ts
import { Component, Input } from '@angular/core'
import { IonicModule, ModalController } from '@ionic/angular'
import { CommonModule } from '@angular/common'
import { FormsModule } from '@angular/forms'
import { AdaosClient } from '../../core/adaos/adaos-client.service'

@Component({
  selector: 'ada-weather-modal',
  standalone: true,
  imports: [IonicModule, CommonModule, FormsModule],
  templateUrl: './weather-modal.component.html',
  styles: [
    `:host{display:flex;flex-direction:column;height:100%} ion-content{flex:1 1 auto}`
  ]
})
export class WeatherModalComponent {
  @Input() title = '??????'
  @Input() weather?: {
    city: string
    temp_c: number
    condition: string
    wind_ms: number
    updated_at: string
  }
  cities: string[] = ['Berlin', 'Moscow', 'New York', 'Tokyo', 'Paris']

  constructor(
    private modalCtrl: ModalController,
    private adaos: AdaosClient
  ) {}

  close() {
    this.modalCtrl.dismiss()
  }

  async onCityChange(city: string): Promise<void> {
    if (!city) return
    if (this.weather) {
      this.weather = { ...this.weather, city }
    }
    // Сигнализируем об изменении города через доменное событие –
    // снапшот и YDoc обновит backend-скилл weather_skill.
    try {
      await this.adaos.sendEventsCommand('weather.city_changed', { city })
    } catch {
      // best-effort; если команда не прошла, останемся на локальном значении
    }
  }
}
