import { mount } from 'svelte'
import './app.css'
import App from './App.svelte'
import { installTooltips } from './lib/tooltip'
import { syncTooltipPreference } from './lib/tooltipprefs.svelte'

installTooltips()
// Settings › General › Show tooltips turns them on and off live
syncTooltipPreference()

const app = mount(App, {
  target: document.getElementById('app')!,
})

export default app
