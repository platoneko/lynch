package io.lynch.tailfwd

import android.Manifest
import android.app.Activity
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.text.InputType
import android.view.View
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import android.widget.Toast
import tsnetlib.Tsnetlib

class MainActivity : Activity() {
    private val main = Handler(Looper.getMainLooper())
    private lateinit var settings: Settings
    private lateinit var statusText: TextView
    private lateinit var dialText: TextView
    private lateinit var loginButton: Button
    private lateinit var toggleButton: Button
    private lateinit var targetInput: EditText
    private lateinit var portInput: EditText
    private var authUrl: String? = null

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        settings = Settings(this)
        buildViews()
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
            running -> s.headline(settings.target)
            ForwardService.startError != null -> "启动失败：${ForwardService.startError}"
            else -> "已停止"
        }
        authUrl = s.authUrl.takeIf { running }
        loginButton.visibility = if (authUrl != null) View.VISIBLE else View.GONE
        val failure = s.dialFailure()
        dialText.text = failure.orEmpty()
        dialText.visibility = if (failure != null) View.VISIBLE else View.GONE
        toggleButton.text = if (running) "停止" else "启动"
        main.postDelayed(::refresh, 500)
    }

    // ── Step 2: start / stop ──

    private fun toggle() {
        if (ForwardService.running) {
            ForwardService.stop(this)
            return
        }
        askNotificationPermission()
        ForwardService.start(this)
    }

    // Without the permission the forward still runs; only its notification is hidden.
    private fun askNotificationPermission() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU &&
            checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED
        ) {
            requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), 0)
        }
    }

    // ── Step 3: settings ──

    private fun save() {
        val target = targetInput.text.toString().trim()
        val port = portInput.text.toString().trim().toIntOrNull()
        if (target.isEmpty() || port == null || port !in 1024..65535) {
            Toast.makeText(this, "目标地址不能为空，本机端口需在 1024–65535 之间", Toast.LENGTH_LONG).show()
            return
        }
        settings.target = target
        settings.port = port
        if (ForwardService.running) ForwardService.restart(this)
    }

    private fun buildViews() {
        val pad = (16 * resources.displayMetrics.density).toInt()
        statusText = TextView(this).apply { textSize = 16f }
        dialText = TextView(this).apply { visibility = View.GONE; setPadding(0, pad / 2, 0, 0) }
        loginButton = Button(this).apply {
            text = "登录 Tailscale"
            visibility = View.GONE
            setOnClickListener { authUrl?.let { startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(it))) } }
        }
        toggleButton = Button(this).apply { setOnClickListener { toggle() } }
        targetInput = EditText(this).apply { setSingleLine(); setText(settings.target) }
        portInput = EditText(this).apply {
            setSingleLine()
            inputType = InputType.TYPE_CLASS_NUMBER
            setText(settings.port.toString())
        }
        val save = Button(this).apply { text = "保存"; setOnClickListener { save() } }
        val panel = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(pad, pad * 3, pad, pad)
            addView(statusText)
            addView(dialText)
            addView(loginButton)
            addView(toggleButton)
            addView(label("目标地址（tailnet 主机:端口）", pad))
            addView(targetInput)
            addView(label("本机端口", pad))
            addView(portInput)
            addView(save)
        }
        // fitsSystemWindows pads the content clear of the bars that targetSdk 35 draws over.
        setContentView(ScrollView(this).apply { fitsSystemWindows = true; addView(panel) })
    }

    private fun label(text: String, pad: Int) = TextView(this).apply {
        this.text = text
        setPadding(0, pad, 0, 0)
    }
}
