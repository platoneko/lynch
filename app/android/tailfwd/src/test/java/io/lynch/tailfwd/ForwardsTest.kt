package io.lynch.tailfwd

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class ForwardsTest {
    private fun ids(): () -> String {
        var n = 0
        return { "id${++n}" }
    }

    private fun fwd(id: String, name: String = id, port: Int = 20000, target: String = "pivot-home:6768", enabled: Boolean = true) =
        Forward(id, name, port, target, enabled)

    @Test
    fun newInstallGetsOrcaAndLynch() {
        assertEquals(
            listOf(
                Forward("id1", "Orca", 16768, "pivot-home.tail090ab2.ts.net:6768", true),
                Forward("id2", "lynch", 31510, "pivot-home.tail090ab2.ts.net:3151", true),
            ),
            Forwards.initial(null, null, ids()),
        )
    }

    @Test
    fun upgradeKeepsTheOldForwardAsOrca() {
        val list = Forwards.initial("other-host.ts.net:7000", 17000, ids())
        assertEquals(Forward("id1", "Orca", 17000, "other-host.ts.net:7000", true), list[0])
        assertEquals(Forward("id2", "lynch", 31510, "pivot-home.tail090ab2.ts.net:3151", true), list[1])
    }

    @Test
    fun upgradeWithOnlyAPortSavedKeepsTheDefaultTarget() {
        assertEquals(Forward("id1", "Orca", 17000, "pivot-home.tail090ab2.ts.net:6768", true), Forwards.initial(null, 17000, ids())[0])
    }

    @Test
    fun aValidForwardHasNoProblem() {
        assertNull(Forwards.problem(fwd("a", port = 20001), listOf(fwd("b", port = 20002))))
    }

    @Test
    fun nameMustBeSetAndUnique() {
        assertEquals("名字不能为空", Forwards.problem(fwd("a", name = " "), emptyList()))
        assertEquals(
            "已有名为「Orca」的连接",
            Forwards.problem(fwd("a", name = "Orca", port = 20001), listOf(fwd("b", name = "Orca", port = 20002))),
        )
    }

    @Test
    fun portMustBeInRangeAndUnusedEvenByADisabledForward() {
        for (p in listOf(-1, 0, 1023, 65536)) {
            assertEquals("本机端口需在 1024–65535 之间", Forwards.problem(fwd("a", port = p), emptyList()))
        }
        assertEquals(
            "本机端口 31510 已被「lynch」使用",
            Forwards.problem(fwd("a", port = 31510), listOf(fwd("b", name = "lynch", port = 31510, enabled = false))),
        )
    }

    @Test
    fun targetMustBeHostColonPort() {
        for (t in listOf("", "pivot-home", "pivot-home:", ":6768", "pivot-home:http", "pivot-home:0", "pivot-home:65536",
            "ws://pivot-home:6768", "pivot-home:6768/", "fd7a::1:80")) {
            assertEquals(t, "目标要写成 主机:端口（端口 1–65535）", Forwards.problem(fwd("a", target = t), emptyList()))
        }
        for (t in listOf("pivot-home.tail090ab2.ts.net:6768", "100.93.210.104:3151", "[fd7a::1]:80")) {
            assertNull(t, Forwards.problem(fwd("a", target = t), emptyList()))
        }
    }

    @Test
    fun planOpensWhatIsWantedAndNotOpen() {
        val a = fwd("a", port = 20001)
        assertEquals(Forwards.Plan(emptyList(), listOf(a)), Forwards.plan(emptyMap(), listOf(a)))
    }

    @Test
    fun planClosesWhatIsNoLongerWanted() {
        val a = fwd("a", port = 20001)
        assertEquals(Forwards.Plan(listOf("a"), emptyList()), Forwards.plan(mapOf("a" to a), emptyList()))
    }

    @Test
    fun planReopensOnPortOrTargetChange() {
        val a = fwd("a", port = 20001)
        for (changed in listOf(a.copy(port = 20002), a.copy(target = "other:1"))) {
            assertEquals(Forwards.Plan(listOf("a"), listOf(changed)), Forwards.plan(mapOf("a" to a), listOf(changed)))
        }
    }

    @Test
    fun planLeavesANameOnlyEditAlone() {
        val a = fwd("a", port = 20001)
        assertEquals(Forwards.Plan(emptyList(), emptyList()), Forwards.plan(mapOf("a" to a), listOf(a.copy(name = "renamed"))))
    }

    // A forward whose open failed (e.g. port taken) is not open, so switching it on again retries it.
    @Test
    fun planReopensAForwardThatIsNotOpen() {
        val a = fwd("a", port = 20001)
        val b = fwd("b", port = 20002)
        assertEquals(Forwards.Plan(emptyList(), listOf(b)), Forwards.plan(mapOf("a" to a), listOf(a, b)))
    }

    @Test
    fun planMovesAPortFromADeletedForwardToANewOne() {
        val a = fwd("a", port = 20001)
        val b = fwd("b", port = 20001)
        assertEquals(Forwards.Plan(listOf("a"), listOf(b)), Forwards.plan(mapOf("a" to a), listOf(b)))
    }
}
