package io.lynch.tailfwd

/**
 * One named forward: 127.0.0.1:[port] on the phone → [target] on the tailnet. [id] is fixed at
 * creation and is what the service and Tsnetlib track; [name] is only for people.
 */
data class Forward(val id: String, val name: String, val port: Int, val target: String, val enabled: Boolean)

/** The forward list's rules, free of Android classes so plain JUnit can test them. */
object Forwards {
    const val ORCA_PORT = 16768 // Orca Mobile already stores ws://127.0.0.1:16768 for pivot
    const val ORCA_TARGET = "pivot-home.tail090ab2.ts.net:6768"
    const val LYNCH_PORT = 31510 // the lynch app's default port
    const val LYNCH_TARGET = "pivot-home.tail090ab2.ts.net:3151"

    /**
     * The list a new install starts with, which is also what an upgrade from the one-forward
     * version gets: that forward (its saved target / port, else the old defaults) becomes Orca,
     * and lynch is added.
     */
    fun initial(oldTarget: String?, oldPort: Int?, newId: () -> String): List<Forward> = listOf(
        Forward(newId(), "Orca", oldPort ?: ORCA_PORT, oldTarget ?: ORCA_TARGET, enabled = true),
        Forward(newId(), "lynch", LYNCH_PORT, LYNCH_TARGET, enabled = true),
    )

    /** Why [f] cannot be saved next to [others] (every other forward, enabled or not), or null. */
    fun problem(f: Forward, others: List<Forward>): String? {
        val samePort = others.firstOrNull { it.port == f.port }
        return when {
            f.name.isBlank() -> "名字不能为空"
            others.any { it.name == f.name } -> "已有名为「${f.name}」的连接"
            f.port !in 1024..65535 -> "本机端口需在 1024–65535 之间"
            samePort != null -> "本机端口 ${f.port} 已被「${samePort.name}」使用"
            !isTarget(f.target) -> "目标要写成 主机:端口（端口 1–65535）"
            else -> null
        }
    }

    /**
     * "host:port" with a non-empty host and a port in 1–65535, an IPv6 host bracketed and no '/'
     * (a pasted URL): checked at save, so the list never holds a target tsnetlib refuses at open.
     */
    fun isTarget(s: String): Boolean {
        val colon = s.lastIndexOf(':')
        if (colon <= 0 || '/' in s) return false
        val host = s.substring(0, colon)
        // Go's net.SplitHostPort takes a ':' in the host only inside "[...]".
        if (':' in host && !(host.startsWith('[') && host.endsWith(']'))) return false
        val port = s.substring(colon + 1).toIntOrNull() ?: return false
        return port in 1..65535
    }

    /** The Tsnetlib calls that make the open forwards match the wanted ones; closes run first. */
    data class Plan(val close: List<String>, val open: List<Forward>)

    /**
     * [open]: what Tsnetlib has open now, by id, as it was opened. [wanted]: the enabled forwards.
     * One whose port or target changed is closed and reopened; a name-only edit is left alone; one
     * that is wanted but not open (new, switched on, or failed to open before) is opened.
     */
    fun plan(open: Map<String, Forward>, wanted: List<Forward>): Plan {
        val byId = wanted.associateBy { it.id }
        return Plan(
            close = open.values.filter { o -> byId[o.id]?.let(::socket) != socket(o) }.map { it.id },
            open = wanted.filter { w -> open[w.id]?.let(::socket) != socket(w) },
        )
    }

    private fun socket(f: Forward) = f.port to f.target
}
