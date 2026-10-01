package io.lynch.tailnet

import android.content.Context
import android.net.ConnectivityManager
import android.net.LinkProperties
import android.net.Network
import android.util.Log

/**
 * Feeds the phone's interface list to a tsnet node. Since Android 11 Go cannot
 * enumerate interfaces itself, so the list is pushed before the node starts and
 * again on every default-network change (e.g. Wi-Fi → mobile data).
 */
class InterfaceFeed(context: Context, private val push: (String) -> Unit) {
    private val cm = context.getSystemService(ConnectivityManager::class.java)
    private val callback = object : ConnectivityManager.NetworkCallback() {
        override fun onAvailable(network: Network) = pushLater()
        override fun onLost(network: Network) = pushLater()
        override fun onLinkPropertiesChanged(network: Network, lp: LinkProperties) = pushLater()
    }

    /** Pushes the current list on the calling thread; call before starting the node. */
    fun pushNow() = push(NetInterfaces.json())

    fun watch() = cm.registerDefaultNetworkCallback(callback)

    fun unwatch() = cm.unregisterNetworkCallback(callback)

    private fun pushLater() {
        Thread {
            runCatching { pushNow() }.onFailure { Log.w(TAG, "pushing interfaces failed", it) }
        }.start()
    }

    internal companion object {
        const val TAG = "tailnet"
    }
}
