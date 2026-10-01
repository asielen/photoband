// Keeps the tooltip system in step with Settings › General › "Show tooltips".
import { app } from './store.svelte'
import { setTooltipsEnabled } from './tooltip'

/** Tooltips are on unless the setting is explicitly off (also while settings are loading). */
export function tooltipsWanted(settings: { general?: { showTooltips?: boolean } } | null | undefined): boolean {
  return settings?.general?.showTooltips !== false
}

/** Watches the setting for the life of the app. Returns the cleanup function. */
export function syncTooltipPreference(): () => void {
  return $effect.root(() => {
    $effect(() => {
      setTooltipsEnabled(tooltipsWanted(app.settings))
    })
  })
}
