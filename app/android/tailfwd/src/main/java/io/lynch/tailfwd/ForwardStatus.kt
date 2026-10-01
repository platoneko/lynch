package io.lynch.tailfwd

import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import org.json.JSONObject

/** Tsnetlib.status(), worded for the notification and the main screen. */
class ForwardStatus(
    private val state: String,
    val authUrl: String?,
    private val error: String?,
    private val open: Map<String, LiveForward>,
) {
    /** A forward Tsnetlib has open, with its last failed dial (null / 0 if none failed). */
    class LiveForward(val dialError: String?, val dialErrorAt: Long)

    val openCount: Int get() = open.size

    fun isOpen(id: String) = id in open

    /** One line for the node while the master switch is on. */
    fun headline(): String = when (state) {
        "running" -> "已连接"
        "needs_login" -> "需要登录 Tailscale"
        "needs_approval" -> "等待在 Tailscale 管理后台批准此设备"
        "error" -> "出错：$error"
        else -> "连接中…" // "stopped" too: the service is on but the node is not up yet
    }

    /** What went wrong with [f]: its failed open ([openError], from the service), then its last failed dial. */
    fun problems(f: Forward, openError: String?): List<String> = listOfNotNull(
        openError?.let { "启动失败：$it" },
        open[f.id]?.let { live ->
            live.dialError?.let {
                val at = SimpleDateFormat("HH:mm:ss", Locale.ROOT).format(Date(live.dialErrorAt))
                "最近拨号失败（$at）：$it"
            }
        },
    )

    companion object {
        fun parse(json: String): ForwardStatus {
            val o = JSONObject(json)
            val forwards = o.optJSONArray("forwards")
            val open = (0 until (forwards?.length() ?: 0)).associate { i ->
                val f = forwards!!.getJSONObject(i)
                f.getString("id") to LiveForward(f.optString("dial_error").ifEmpty { null }, f.optLong("dial_error_at"))
            }
            return ForwardStatus(
                state = o.optString("state"),
                authUrl = o.optString("auth_url").ifEmpty { null },
                error = o.optString("error").ifEmpty { null },
                open = open,
            )
        }
    }
}
