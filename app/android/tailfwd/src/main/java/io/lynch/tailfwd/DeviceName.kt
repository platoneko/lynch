package io.lynch.tailfwd

/** The node's name on the tailnet (what the Tailscale admin console lists). */
object DeviceName {
    /** The fixed name of the one-forward version's node. */
    const val LEGACY = "tailfwd-phone"
    private const val PREFIX = "tailfwd-"
    private const val MAX = 63 // one DNS label
    private val VALID = Regex("[a-z0-9]([a-z0-9-]*[a-z0-9])?")

    /**
     * The name a first start uses. An install that already has a node keeps [LEGACY], so the
     * device keeps its name in the admin console; otherwise `tailfwd-` plus the phone's name, or
     * its model when the name has no usable characters (e.g. all Chinese).
     */
    fun default(hasNodeState: Boolean, phoneName: String?, model: String?): String {
        if (hasNodeState) return LEGACY
        val label = label(phoneName.orEmpty()).ifEmpty { label(model.orEmpty()) }
        if (label.isEmpty()) return LEGACY
        return (PREFIX + label).take(MAX).trimEnd('-')
    }

    /** Why [name] is not a usable device name, or null. */
    fun problem(name: String): String? = when {
        name.isEmpty() -> "设备名不能为空"
        name.length > MAX -> "设备名最长 $MAX 个字符"
        !VALID.matches(name) -> "设备名只能用小写字母、数字和 -，且不能以 - 开头或结尾"
        else -> null
    }

    // Lower-case; every run of anything but [a-z0-9] becomes one "-"; no "-" at either end.
    private fun label(s: String) = s.lowercase().replace(Regex("[^a-z0-9]+"), "-").trim('-')
}
