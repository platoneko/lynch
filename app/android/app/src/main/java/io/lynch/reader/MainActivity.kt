package io.lynch.reader

import android.app.Activity
import android.content.Intent
import android.net.ConnectivityManager
import android.net.LinkProperties
import android.net.Network
import android.net.Uri
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.text.InputType
import android.util.Log
import android.view.View
import android.view.ViewGroup.LayoutParams.MATCH_PARENT
import android.view.ViewGroup.LayoutParams.WRAP_CONTENT
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.Button
import android.widget.EditText
import android.widget.FrameLayout
import android.widget.LinearLayout
import android.widget.TextView
import java.io.File
import org.json.JSONObject
import tsnetlib.Tsnetlib

class MainActivity : Activity() {
    private val main = Handler(Looper.getMainLooper())
    private lateinit var panel: LinearLayout
    private lateinit var statusText: TextView
    private lateinit var loginButton: Button
    private lateinit var retryButton: Button
    private lateinit var addressInput: EditText
    private lateinit var portInput: EditText
    private lateinit var web: WebView
    private var authUrl: String? = null
    private var loadedPort = 0 // proxy port the WebView was booted against; 0 = not loaded
    private var clearHistoryOnLoad = false // set when a new proxy session is booted

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        buildViews()
        watchNetwork()
        startTailnet()
    }

    // ── Step 1: bring up the node (once per process) and poll its state ──

    private fun startTailnet() {
        showPanel("连接中…")
        Thread {
            val err = runCatching {
                Tsnetlib.setInterfaces(NetInterfaces.json())
                // Activity re-creation must not start a second node.
                if (JSONObject(Tsnetlib.status()).optString("state") == "stopped") {
                    Tsnetlib.start(File(filesDir, "tailnet").absolutePath, serverAddress(), localPort().toLong())
                }
            }.exceptionOrNull()
            main.post { if (err != null) showError(err.message ?: err.toString()) else poll() }
        }.start()
    }

    private fun poll() {
        val s = JSONObject(Tsnetlib.status())
        when (s.optString("state")) {
            "running" -> {
                openWeb(s.getInt("port"))
                return // stop polling; a later outage shows up as the proxy's 502 page
            }
            "needs_login" -> {
                authUrl = s.optString("auth_url").ifEmpty { null }
                showPanel(if (authUrl == null) "正在获取登录链接…" else "需要登录 Tailscale", login = authUrl != null)
            }
            "needs_approval" -> showPanel("等待在 Tailscale 管理后台批准此设备")
            "error" -> showError(s.optString("error"))
            else -> showPanel("连接中…")
        }
        main.postDelayed(::poll, 500)
    }

    private fun restartTailnet() {
        main.removeCallbacksAndMessages(null)
        loadedPort = 0
        Thread {
            Tsnetlib.stop()
            main.post { startTailnet() }
        }.start()
    }

    private fun watchNetwork() {
        val cm = getSystemService(ConnectivityManager::class.java)
        cm.registerDefaultNetworkCallback(object : ConnectivityManager.NetworkCallback() {
            override fun onAvailable(network: Network) = refreshInterfaces()
            override fun onLost(network: Network) = refreshInterfaces()
            override fun onLinkPropertiesChanged(network: Network, lp: LinkProperties) = refreshInterfaces()
        })
    }

    private fun refreshInterfaces() {
        Thread {
            runCatching { Tsnetlib.setInterfaces(NetInterfaces.json()) }
                .onFailure { Log.w(TAG, "setInterfaces failed", it) }
        }.start()
    }

    // ── Step 2: WebView pinned to the loopback proxy ──

    private fun openWeb(port: Int) {
        if (loadedPort != port) {
            loadedPort = port
            clearHistoryOnLoad = true
            web.loadUrl(Tsnetlib.bootURL())
        }
        panel.visibility = View.GONE
        web.visibility = View.VISIBLE
    }

    private fun configureWeb(w: WebView) {
        w.settings.javaScriptEnabled = true
        w.settings.allowFileAccess = false
        w.settings.allowContentAccess = false
        w.webViewClient = object : WebViewClient() {
            // true = block: only our proxy origin may load in the WebView.
            override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
                val u = request.url
                return !(u.scheme == "http" && u.host == "127.0.0.1" && u.port == loadedPort)
            }

            // Each restart boots a new proxy session; once its first page commits, drop the old
            // session's pages so Back does not walk into them.
            override fun doUpdateVisitedHistory(view: WebView, url: String?, isReload: Boolean) {
                if (clearHistoryOnLoad && Uri.parse(url.orEmpty()).port == loadedPort) {
                    clearHistoryOnLoad = false
                    view.clearHistory()
                }
            }
        }
    }

    @Deprecated("Deprecated in Java")
    override fun onBackPressed() {
        when {
            web.visibility == View.VISIBLE && web.canGoBack() -> web.goBack()
            // Root of the reader: show status/settings, the only place to change the server.
            web.visibility == View.VISIBLE -> showPanel("已连接")
            // Settings over a live reader: put the reader back, then leave.
            loadedPort != 0 -> {
                panel.visibility = View.GONE
                web.visibility = View.VISIBLE
                moveTaskToBack(true)
            }
            else -> moveTaskToBack(true)
        }
    }

    // ── Step 3: status / settings panel ──

    private fun prefs() = getSharedPreferences("lynch", MODE_PRIVATE)
    private fun serverAddress(): String = prefs().getString(KEY_SERVER, null) ?: DEFAULT_SERVER
    private fun localPort(): Int = prefs().getInt(KEY_PORT, DEFAULT_PORT)

    // The WebView is hidden, never destroyed, so the reader keeps its page state.
    private fun showPanel(text: String, login: Boolean = false) {
        statusText.text = text
        loginButton.visibility = if (login) View.VISIBLE else View.GONE
        retryButton.visibility = View.GONE
        panel.visibility = View.VISIBLE
        web.visibility = View.GONE
    }

    private fun showError(message: String) {
        showPanel("连接失败：$message")
        retryButton.visibility = View.VISIBLE
    }

    private fun buildViews() {
        val pad = (16 * resources.displayMetrics.density).toInt()
        statusText = TextView(this).apply { textSize = 16f }
        loginButton = Button(this).apply {
            text = "登录 Tailscale"
            setOnClickListener { authUrl?.let { startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(it))) } }
        }
        retryButton = Button(this).apply { text = "重试"; setOnClickListener { restartTailnet() } }
        addressInput = EditText(this).apply { setSingleLine(); setText(serverAddress()) }
        portInput = EditText(this).apply {
            setSingleLine()
            inputType = InputType.TYPE_CLASS_NUMBER
            setText(localPort().toString())
        }
        val save = Button(this).apply {
            text = "保存并重连"
            setOnClickListener {
                val port = portInput.text.toString().trim().toIntOrNull()
                if (port == null || port !in 1024..65535) {
                    showError("本机端口需在 1024–65535 之间")
                    return@setOnClickListener
                }
                prefs().edit()
                    .putString(KEY_SERVER, addressInput.text.toString().trim())
                    .putInt(KEY_PORT, port)
                    .apply()
                restartTailnet()
            }
        }
        panel = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(pad, pad * 3, pad, pad)
            addView(statusText)
            addView(loginButton)
            addView(retryButton)
            addView(TextView(this@MainActivity).apply { text = "服务器地址"; setPadding(0, pad, 0, 0) })
            addView(addressInput)
            addView(TextView(this@MainActivity).apply { text = "本机端口"; setPadding(0, pad, 0, 0) })
            addView(portInput)
            addView(save)
        }
        web = WebView(this).apply { visibility = View.GONE }
        configureWeb(web)
        setContentView(FrameLayout(this).apply {
            addView(web, FrameLayout.LayoutParams(MATCH_PARENT, MATCH_PARENT))
            addView(panel, FrameLayout.LayoutParams(MATCH_PARENT, WRAP_CONTENT))
        })
    }

    companion object {
        const val TAG = "lynch"
        private const val KEY_SERVER = "server"
        private const val DEFAULT_SERVER = "pivot-home.tail090ab2.ts.net:3151"
        private const val KEY_PORT = "port"
        private const val DEFAULT_PORT = 31510
    }
}
