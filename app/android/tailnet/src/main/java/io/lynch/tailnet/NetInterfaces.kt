package io.lynch.tailnet

import android.util.Log
import java.net.NetworkInterface
import org.json.JSONArray
import org.json.JSONObject

/** Interface rows for tsnetlib.SetInterfaces; Go cannot enumerate them itself on Android 11+. */
internal object NetInterfaces {
    // Bit values of Go's net.Flags (net/interface.go).
    private const val UP = 1
    private const val LOOPBACK = 4
    private const val POINT_TO_POINT = 8
    private const val MULTICAST = 16
    private const val RUNNING = 32

    fun json(): String {
        val rows = JSONArray()
        val all = NetworkInterface.getNetworkInterfaces()?.toList().orEmpty()
        for (ni in all) {
            val row = runCatching { row(ni) }
                .onFailure { Log.w(InterfaceFeed.TAG, "skipping interface ${ni.name}", it) }
                .getOrNull() ?: continue
            rows.put(row)
        }
        return rows.toString()
    }

    private fun row(ni: NetworkInterface): JSONObject {
        var flags = 0
        if (ni.isUp) flags = flags or UP or RUNNING
        if (ni.isLoopback) flags = flags or LOOPBACK
        if (ni.isPointToPoint) flags = flags or POINT_TO_POINT
        if (ni.supportsMulticast()) flags = flags or MULTICAST
        val addrs = JSONArray()
        for (ia in ni.interfaceAddresses) {
            // IPv6 host strings carry a %scope suffix that Go's ParseCIDR rejects.
            val host = ia.address.hostAddress?.substringBefore('%') ?: continue
            addrs.put("$host/${ia.networkPrefixLength}")
        }
        return JSONObject()
            .put("name", ni.name)
            .put("index", ni.index)
            .put("mtu", ni.mtu)
            .put("flags", flags)
            .put("addresses", addrs)
    }
}
