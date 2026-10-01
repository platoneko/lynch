package io.lynch.tailfwd

import android.content.Context
import android.os.Build
import android.provider.Settings.Global
import java.io.File
import java.util.UUID
import org.json.JSONArray
import org.json.JSONObject

/** The forward list and the device name: the screen edits them, the service reads them. */
class Settings(private val context: Context) {
    private val prefs = context.getSharedPreferences("tailfwd", Context.MODE_PRIVATE)

    var forwards: List<Forward>
        get() = prefs.getString(KEY_FORWARDS, null)?.let(::decode) ?: migrate()
        set(value) = prefs.edit().putString(KEY_FORWARDS, encode(value)).apply()

    /**
     * The node's name. The default is computed once and stored, so it never changes behind the
     * user's back — in particular not to [DeviceName.LEGACY] once the first start creates node state.
     */
    var hostname: String
        get() = prefs.getString(KEY_HOSTNAME, null) ?: DeviceName.default(
            hasNodeState = File(context.filesDir, "tailnet/tsnet").exists(),
            phoneName = Global.getString(context.contentResolver, Global.DEVICE_NAME),
            model = Build.MODEL,
        ).also { hostname = it }
        set(value) = prefs.edit().putString(KEY_HOSTNAME, value).apply()

    /** Saves [f], replacing the forward with its id or adding it at the end. */
    fun put(f: Forward) {
        val list = forwards
        forwards = if (list.any { it.id == f.id }) list.map { if (it.id == f.id) f else it } else list + f
    }

    fun remove(id: String) {
        forwards = forwards.filterNot { it.id == id }
    }

    // The one-forward version kept "target" / "port"; they become the Orca entry, once.
    private fun migrate(): List<Forward> {
        val oldPort = if (prefs.contains(KEY_V1_PORT)) prefs.getInt(KEY_V1_PORT, Forwards.ORCA_PORT) else null
        val list = Forwards.initial(prefs.getString(KEY_V1_TARGET, null), oldPort) { UUID.randomUUID().toString() }
        prefs.edit().putString(KEY_FORWARDS, encode(list)).remove(KEY_V1_TARGET).remove(KEY_V1_PORT).apply()
        return list
    }

    private fun encode(list: List<Forward>): String = JSONArray(
        list.map {
            JSONObject().put("id", it.id).put("name", it.name).put("port", it.port)
                .put("target", it.target).put("enabled", it.enabled)
        },
    ).toString()

    private fun decode(json: String): List<Forward> {
        val a = JSONArray(json)
        return (0 until a.length()).map { i ->
            val o = a.getJSONObject(i)
            Forward(o.getString("id"), o.getString("name"), o.getInt("port"), o.getString("target"), o.getBoolean("enabled"))
        }
    }

    private companion object {
        const val KEY_FORWARDS = "forwards"
        const val KEY_HOSTNAME = "hostname"
        const val KEY_V1_TARGET = "target"
        const val KEY_V1_PORT = "port"
    }
}
