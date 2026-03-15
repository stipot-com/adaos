import { addIcons } from 'ionicons'
import {
  lockClosedOutline,
  people,
  phonePortrait,
  apps,
  laptop,
  desktop,
  settings,
  close,
  image,
  camera,
  refresh,
  cloudOutline,
  appsOutline,
  contractOutline,
  expandOutline,
} from 'ionicons/icons'

export function registerIcons(): void {
  addIcons({
    'lock-closed-outline': lockClosedOutline,
    people,
    'phone-portrait': phonePortrait,
    apps,
    laptop,
    desktop,
    settings,
    close,
    image,
    camera,
    refresh,
    'cloud-outline': cloudOutline,
    'apps-outline': appsOutline,
    'contract-outline': contractOutline,
    'expand-outline': expandOutline,
  })
}
