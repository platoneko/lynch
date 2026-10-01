package io.lynch.tailfwd

import android.content.Context

/** The one forward's settings: the screen edits them, the service reads them at each start. */
class Settings(context: Context) {
    private val prefs = context.getSharedPreferences("tailfwd", Context.MODE_PRIVATE)

    var target: String
        get() = prefs.getString(KEY_TARGET, null) ?: DEFAULT_TARGET
        set(value) = prefs.edit().putString(KEY_TARGET, value).apply()

    var port: Int
        get() = prefs.getInt(KEY_PORT, DEFAULT_PORT)
        set(value) = prefs.edit().putInt(KEY_PORT, value).apply()

    private companion object {
        const val KEY_TARGET = "target"
        const val KEY_PORT = "port"
        const val DEFAULT_TARGET = "pivot-home.tail090ab2.ts.net:6768"
        // Orca Mobile already stores ws://127.0.0.1:16768 for pivot; keeping it avoids re-pairing.
        const val DEFAULT_PORT = 16768
    }
}
