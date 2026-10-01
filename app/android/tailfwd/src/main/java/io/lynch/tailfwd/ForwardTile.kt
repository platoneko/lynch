package io.lynch.tailfwd

import android.service.quicksettings.Tile
import android.service.quicksettings.TileService

/** Quick-settings switch for the forward; lit while ForwardService runs. */
class ForwardTile : TileService() {
    override fun onStartListening() = refresh()

    override fun onClick() {
        val starting = !ForwardService.running
        if (starting) ForwardService.start(this) else ForwardService.stop(this)
        // The service cannot redraw a tile that is not ACTIVE_TILE, so show the expected
        // state now; onStartListening re-reads the real one whenever the panel opens.
        qsTile?.let {
            it.state = if (starting) Tile.STATE_ACTIVE else Tile.STATE_INACTIVE
            it.updateTile()
        }
    }

    private fun refresh() {
        val tile = qsTile ?: return
        tile.state = if (ForwardService.running) Tile.STATE_ACTIVE else Tile.STATE_INACTIVE
        tile.updateTile()
    }
}
