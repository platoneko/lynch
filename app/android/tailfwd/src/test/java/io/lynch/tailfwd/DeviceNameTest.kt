package io.lynch.tailfwd

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class DeviceNameTest {
    @Test
    fun englishPhoneName() {
        assertEquals("tailfwd-xiaomi-14-ultra", DeviceName.default(false, "Xiaomi 14 Ultra", "23116PN5BC"))
    }

    @Test
    fun runsOfOtherCharactersBecomeOneDash() {
        assertEquals("tailfwd-neko-s-phone", DeviceName.default(false, "  Neko's  Phone!! ", "x"))
    }

    @Test
    fun aChineseNameFallsBackToTheModel() {
        assertEquals("tailfwd-23116pn5bc", DeviceName.default(false, "我的手机", "23116PN5BC"))
    }

    @Test
    fun noUsableNameAtAllKeepsTheLegacyName() {
        assertEquals("tailfwd-phone", DeviceName.default(false, null, null))
        assertEquals("tailfwd-phone", DeviceName.default(false, "手机", "小米"))
    }

    @Test
    fun longNamesAreCutTo63WithoutATrailingDash() {
        // "tailfwd-" (8) + 54 a + "-" is exactly 63 characters, so the cut lands right after the dash.
        assertEquals("tailfwd-" + "a".repeat(54), DeviceName.default(false, "a".repeat(54) + " " + "b".repeat(20), "x"))
        assertEquals(63, DeviceName.default(false, "c".repeat(100), "x").length)
    }

    @Test
    fun anExistingNodeKeepsItsName() {
        assertEquals("tailfwd-phone", DeviceName.default(true, "Xiaomi 14 Ultra", "23116PN5BC"))
    }

    @Test
    fun everyDefaultIsAValidName() {
        for (phone in listOf("Xiaomi 14 Ultra", "我的手机", "a".repeat(54) + " b", "c".repeat(100), null)) {
            assertNull(DeviceName.problem(DeviceName.default(false, phone, "23116PN5BC")))
        }
    }

    @Test
    fun problems() {
        assertEquals("设备名不能为空", DeviceName.problem(""))
        assertEquals("设备名最长 63 个字符", DeviceName.problem("a".repeat(64)))
        for (bad in listOf("Phone", "-a", "a-", "a_b", "手机", "a b")) {
            assertEquals(bad, "设备名只能用小写字母、数字和 -，且不能以 - 开头或结尾", DeviceName.problem(bad))
        }
        for (ok in listOf("tailfwd-phone", "a", "a-1", "a".repeat(63))) {
            assertNull(ok, DeviceName.problem(ok))
        }
    }
}
