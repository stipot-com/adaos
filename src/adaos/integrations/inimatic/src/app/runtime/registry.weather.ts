import { WidgetRegistry, ModalRegistry } from './registry'
import { WeatherWidgetComponent } from '../renderer/widgets/weather-widget.component'
import { WeatherModalComponent } from '../renderer/modals/weather-modal.component'

WidgetRegistry['weather'] = (cfg: any) => ({
  component: WeatherWidgetComponent,
  inputs: { title: cfg?.title || 'Погода', data: cfg?.data }
})

ModalRegistry['weather'] = (cfg: any) => ({
  component: WeatherModalComponent,
  inputs: { title: cfg?.title || 'Погода', weather: cfg?.data }
})

