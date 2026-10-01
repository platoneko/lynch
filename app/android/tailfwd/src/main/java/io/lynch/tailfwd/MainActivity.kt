package io.lynch.tailfwd

import android.Manifest
import android.app.Activity
import android.app.AlertDialog
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.text.InputType
import android.view.Gravity
import android.view.View
import android.view.ViewGroup.LayoutParams.WRAP_CONTENT
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.Switch
import android.widget.TextView
import android.widget.Toast
import java.util.UUID
import tsnetlib.Tsnetlib

class MainActivity : Activity() {
    private val main = Handler(Looper.getMainLooper())
    private val pad by lazy { (16 * resources.displayMetrics.density).toInt() }
    private lateinit var settings: Settings
    private lateinit var statusText: TextView
    private lateinit var loginButton: Button
    private lateinit var toggleButton: Button
    private lateinit var list: LinearLayout
    private lateinit var hostnameInput: EditText
    private var rows: List<Row> = emptyList()
    private var authUrl: String? = null

    /** One forward's line in the list; [status] is rewritten on every refresh. */
    private class Row(val forward: Forward, val status: TextView)

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        settings = Settings(this)
        buildViews()
        showForwards()
    }

    override fun onResume() {
        super.onResume()
        refresh()
    }

    override fun onPause() {
        main.removeCallbacksAndMessages(null)
        super.onPause()
    }

    // ── Step 1: mirror the service while the screen is visible ──

    private fun refresh() {
        val running = ForwardService.running
        val s = ForwardStatus.parse(Tsnetlib.status())
        statusText.text = when {
            running -> s.headline()
            ForwardService.startError != null -> "启动失败：${ForwardService.startError}"
            else -> "已停止"
        }
        authUrl = s.authUrl.takeIf { running }
        loginButton.visibility = if (authUrl != null) View.VISIBLE else View.GONE
        toggleButton.text = if (running) "停止" else "启动"
        for (row in rows) {
            val text = rowStatus(row.forward, s, running)
            row.status.text = text
            row.status.visibility = if (text.isEmpty()) View.GONE else View.VISIBLE
        }
        main.postDelayed(::refresh, 500)
    }

    // Only a switched-on forward under a running service has anything to report.
    private fun rowStatus(f: Forward, s: ForwardStatus, running: Boolean): String {
        if (!running || !f.enabled) return ""
        val forwarding = listOfNotNull("转发中".takeIf { s.isOpen(f.id) })
        return (forwarding + s.problems(f, ForwardService.openErrors[f.id])).joinToString("\n")
    }

    // ── Step 2: the master switch ──

    private fun toggle() {
        if (ForwardService.running) {
            ForwardService.stop(this)
            return
        }
        askNotificationPermission()
        ForwardService.start(this)
    }

    // Without the permission forwarding still runs; only its notification is hidden.
    private fun askNotificationPermission() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU &&
            checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED
        ) {
            requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), 0)
        }
    }

    // ── Step 3: the forward list ──

    private fun showForwards() {
        list.removeAllViews()
        rows = settings.forwards.map { f -> Row(f, TextView(this)) }
        rows.forEach { list.addView(rowView(it)) }
    }

    private fun rowView(row: Row): View {
        val f = row.forward
        val switch = Switch(this).apply {
            isChecked = f.enabled
            setOnCheckedChangeListener { _, on ->
                settings.put(f.copy(enabled = on))
                changed()
            }
        }
        val texts = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(pad / 2, 0, 0, 0)
            addView(TextView(this@MainActivity).apply { text = f.name; textSize = 16f })
            addView(TextView(this@MainActivity).apply { text = "127.0.0.1:${f.port} → ${f.target}" })
            addView(row.status.apply { visibility = View.GONE })
            setOnClickListener { edit(f) }
        }
        return LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
            setPadding(0, pad / 2, 0, pad / 2)
            addView(switch)
            addView(texts, LinearLayout.LayoutParams(0, WRAP_CONTENT, 1f))
        }
    }

    /** The saved list changed: let a running service apply it, and redraw. */
    private fun changed() {
        ForwardService.sync(this)
        main.post(::showForwards) // not inside the switch's own callback
    }

    /** Adds ([old] == null) or edits a forward; a rejected entry keeps the dialog open. */
    private fun edit(old: Forward?) {
        val name = input(old?.name.orEmpty())
        val port = input(old?.port?.toString().orEmpty()).apply { inputType = InputType.TYPE_CLASS_NUMBER }
        val target = input(old?.target.orEmpty())
        val form = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(pad, 0, pad, 0)
            addView(label("名字"))
            addView(name)
            addView(label("本机端口"))
            addView(port)
            addView(label("目标（tailnet 主机:端口）"))
            addView(target)
        }
        val builder = AlertDialog.Builder(this)
            .setTitle(if (old == null) "添加连接" else "编辑连接")
            .setView(form)
            .setPositiveButton("保存", null) // replaced below so a bad entry does not close the dialog
            .setNegativeButton("取消", null)
        if (old != null) {
            builder.setNeutralButton("删除") { _, _ ->
                settings.remove(old.id)
                changed()
            }
        }
        val dialog = builder.create()
        dialog.setOnShowListener {
            dialog.getButton(AlertDialog.BUTTON_POSITIVE).setOnClickListener {
                val f = Forward(
                    id = old?.id ?: UUID.randomUUID().toString(),
                    name = name.text.toString().trim(),
                    port = port.text.toString().trim().toIntOrNull() ?: -1,
                    target = target.text.toString().trim(),
                    enabled = old?.enabled ?: true,
                )
                val problem = Forwards.problem(f, settings.forwards.filterNot { it.id == f.id })
                if (problem != null) {
                    toast(problem)
                    return@setOnClickListener
                }
                settings.put(f)
                changed()
                dialog.dismiss()
            }
        }
        dialog.show()
    }

    // ── Step 4: the device name ──

    private fun saveHostname() {
        val name = hostnameInput.text.toString().trim()
        DeviceName.problem(name)?.let {
            toast(it)
            return
        }
        settings.hostname = name
        toast(if (ForwardService.running) "已保存，下次启动生效" else "已保存")
    }

    private fun buildViews() {
        statusText = TextView(this).apply { textSize = 16f }
        loginButton = Button(this).apply {
            text = "登录 Tailscale"
            visibility = View.GONE
            setOnClickListener { authUrl?.let { startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(it))) } }
        }
        toggleButton = Button(this).apply { setOnClickListener { toggle() } }
        list = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        val add = Button(this).apply { text = "添加连接"; setOnClickListener { edit(null) } }
        hostnameInput = EditText(this).apply { setSingleLine(); setText(settings.hostname) }
        val saveHostname = Button(this).apply { text = "保存设备名"; setOnClickListener { saveHostname() } }
        val panel = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(pad, pad * 3, pad, pad)
            addView(statusText)
            addView(loginButton)
            addView(toggleButton)
            addView(label("连接"))
            addView(list)
            addView(add)
            addView(label("设备名（Tailscale 里显示的名字）"))
            addView(hostnameInput)
            addView(saveHostname)
        }
        // fitsSystemWindows pads the content clear of the bars that targetSdk 35 draws over.
        setContentView(ScrollView(this).apply { fitsSystemWindows = true; addView(panel) })
    }

    private fun input(value: String) = EditText(this).apply { setSingleLine(); setText(value) }

    private fun label(text: String) = TextView(this).apply {
        this.text = text
        setPadding(0, pad, 0, 0)
    }

    private fun toast(text: String) = Toast.makeText(this, text, Toast.LENGTH_LONG).show()
}
