package io.lynch.tailfwd

import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import org.json.JSONObject

/** Tsnetlib.status(), worded for the notification and the main screen. */
class ForwardStatus(
    private val state: String,
    val authUrl: String?,
    private val port: Int,
    private val error: String?,
    private val dialError: String?,
    private val dialErrorAt: Long,
) {
    /** One line for a forward the user has switched on. */
    fun headline(target: String): String = when (state) {
        "running" -> "已连接 · 127.0.0.1:$port → $target"
        "needs_login" -> "需要登录 Tailscale"
        "needs_approval" -> "等待在 Tailscale 管理后台批准此设备"
        "error" -> "出错：$error"
        else -> "连接中…" // "stopped" too: the service is on but the node is not up yet
    }

    /** The last failed dial to the target, or null if none has failed. */
    fun dialFailure(): String? = dialError?.let {
        val at = SimpleDateFormat("HH:mm:ss", Locale.ROOT).format(Date(dialErrorAt))
        "最近拨号失败（$at）：$it"
    }

    companion object {
        fun parse(json: String): ForwardStatus {
            val o = JSONObject(json)
            return ForwardStatus(
                state = o.optString("state"),
                authUrl = o.optString("auth_url").ifEmpty { null },
                port = o.optInt("port"),
                error = o.optString("error").ifEmpty { null },
                dialError = o.optString("dial_error").ifEmpty { null },
                dialErrorAt = o.optLong("dial_error_at"),
            )
        }
    }
}
