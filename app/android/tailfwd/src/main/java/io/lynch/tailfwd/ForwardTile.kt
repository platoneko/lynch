package io.lynch.tailfwd

import android.service.quicksettings.Tile
import android.service.quicksettings.TileService

/** Quick-settings switch for the forward; lit while ForwardService runs. */
class ForwardTile : TileService() {
    override fun onStartListening() = refresh()

    override fun onClick() {
        if (ForwardService.running) ForwardService.stop(this) else ForwardService.start(this)
    }

    private fun refresh() {
        val tile = qsTile ?: return
        tile.state = if (ForwardService.running) Tile.STATE_ACTIVE else Tile.STATE_INACTIVE
        tile.updateTile()
    }
}
