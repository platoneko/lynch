package io.lynch.reader

import android.app.Activity
import android.content.Intent
import android.graphics.Bitmap
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.text.InputType
import android.view.View
import android.view.ViewGroup.LayoutParams.MATCH_PARENT
import android.view.ViewGroup.LayoutParams.WRAP_CONTENT
import android.view.WindowInsets
import android.webkit.CookieManager
import android.webkit.WebChromeClient
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.Button
import android.widget.EditText
import android.widget.FrameLayout
import android.widget.LinearLayout
import android.widget.TextView
import android.widget.Toast

/**
 * The lynch reader: a WebView pinned to the lynch server, which tailfwd forwards to
 * 127.0.0.1:<port> on the phone. The server pairs this device itself (a cookie bought with a
 * one-time code, kept in the WebView's cookie store); this shell only shows the page, or why
 * nothing answered.
 */
class MainActivity : Activity() {
    private lateinit var panel: LinearLayout
    private lateinit var statusText: TextView
    private lateinit var retryButton: Button
    private lateinit var openTailfwdButton: Button
    private lateinit var portInput: EditText
    private lateinit var web: WebView
    private var port = 0 // the loopback port the WebView is pinned to
    private var mainFrameFailed = false
    private var clearHistoryOnLoad = false // set when the port changes

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        buildViews()
        load(localPort())
    }

    override fun onPause() {
        // The device cookie is the pairing: get it on disk before the process may be killed.
        CookieManager.getInstance().flush()
        super.onPause()
    }

    // ── Step 1: load the reader through tailfwd ──

    private fun load(p: Int) {
        if (p != port) {
            port = p
            clearHistoryOnLoad = true
        }
        showPanel("连接中…")
        web.loadUrl("http://127.0.0.1:$port/")
    }

    private fun retry() {
        showPanel("连接中…")
        web.reload()
    }

    private fun configureWeb(w: WebView) {
        w.settings.javaScriptEnabled = true
        w.settings.allowFileAccess = false
        w.settings.allowContentAccess = false
        w.settings.domStorageEnabled = true // localStorage: chat drafts survive leaving the page
        w.webChromeClient = WebChromeClient() // without one, JS alert/confirm are silently cancelled
        w.webViewClient = object : WebViewClient() {
            // true = block: only the forwarded lynch origin may load in the WebView.
            override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
                val u = request.url
                return !(u.scheme == "http" && u.host == "127.0.0.1" && u.port == port)
            }

            // A port change starts over; once its first page commits, drop the old port's pages
            // so Back does not walk into them.
            override fun doUpdateVisitedHistory(view: WebView, url: String?, isReload: Boolean) {
                if (clearHistoryOnLoad && Uri.parse(url.orEmpty()).port == port) {
                    clearHistoryOnLoad = false
                    view.clearHistory()
                }
            }

            override fun onPageStarted(view: WebView, url: String?, favicon: Bitmap?) {
                mainFrameFailed = false
            }

            // Network-level failure only (an HTTP error status still renders the server's page):
            // tailfwd stopped, its lynch forward off, or nothing behind it. No page of ours can
            // load to say so, hence the native panel.
            override fun onReceivedError(view: WebView, request: WebResourceRequest, error: WebResourceError) {
                if (!request.isForMainFrame) return
                mainFrameFailed = true
                showPanel(
                    "连不上 lynch 服务（127.0.0.1:$port）。请确认 tailfwd 已启动、lynch 连接已启用，并且 pivot 上的服务在运行。",
                    failed = true,
                )
            }

            override fun onPageFinished(view: WebView, url: String?) {
                if (!mainFrameFailed) showWeb()
            }
        }
    }

    @Deprecated("Deprecated in Java")
    override fun onBackPressed() {
        when {
            web.visibility == View.VISIBLE && web.canGoBack() -> web.goBack()
            // Root of the reader: show the panel, the only place to change the port.
            web.visibility == View.VISIBLE -> showPanel("lynch 经 tailfwd 连接 127.0.0.1:$port")
            // Panel over a live reader: put the reader back, then leave.
            !mainFrameFailed -> {
                showWeb()
                moveTaskToBack(true)
            }
            else -> moveTaskToBack(true)
        }
    }

    // ── Step 2: the panel — why the reader is not shown, and the port setting ──

    private fun prefs() = getSharedPreferences("lynch", MODE_PRIVATE)
    private fun localPort(): Int = prefs().getInt(KEY_PORT, DEFAULT_PORT)

    // The WebView is hidden, never destroyed, so the reader keeps its page state.
    private fun showPanel(text: String, failed: Boolean = false) {
        statusText.text = text
        retryButton.visibility = if (failed) View.VISIBLE else View.GONE
        openTailfwdButton.visibility = if (failed && tailfwdLaunch() != null) View.VISIBLE else View.GONE
        panel.visibility = View.VISIBLE
        web.visibility = View.GONE
    }

    private fun showWeb() {
        panel.visibility = View.GONE
        web.visibility = View.VISIBLE
    }

    // Null when tailfwd is not installed (the manifest's <queries> makes it visible when it is).
    private fun tailfwdLaunch(): Intent? = packageManager.getLaunchIntentForPackage(TAILFWD)

    private fun savePort() {
        val p = portInput.text.toString().trim().toIntOrNull()
        if (p == null || p !in 1024..65535) {
            Toast.makeText(this, "本机端口需在 1024–65535 之间", Toast.LENGTH_LONG).show()
            return
        }
        prefs().edit().putInt(KEY_PORT, p).apply()
        load(p)
    }

    private fun buildViews() {
        val pad = (16 * resources.displayMetrics.density).toInt()
        statusText = TextView(this).apply { textSize = 16f }
        retryButton = Button(this).apply { text = "重试"; setOnClickListener { retry() } }
        openTailfwdButton = Button(this).apply {
            text = "打开 tailfwd"
            setOnClickListener { tailfwdLaunch()?.let { startActivity(it) } }
        }
        portInput = EditText(this).apply {
            setSingleLine()
            inputType = InputType.TYPE_CLASS_NUMBER
            setText(localPort().toString())
        }
        val save = Button(this).apply { text = "保存并重新加载"; setOnClickListener { savePort() } }
        panel = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(pad, pad * 3, pad, pad)
            addView(statusText)
            addView(retryButton)
            addView(openTailfwdButton)
            addView(TextView(this@MainActivity).apply { text = "本机端口（tailfwd 里 lynch 连接的端口）"; setPadding(0, pad, 0, 0) })
            addView(portInput)
            addView(save)
        }
        web = WebView(this).apply { visibility = View.GONE }
        configureWeb(web)
        val root = FrameLayout(this).apply {
            addView(web, FrameLayout.LayoutParams(MATCH_PARENT, MATCH_PARENT))
            addView(panel, FrameLayout.LayoutParams(MATCH_PARENT, WRAP_CONTENT))
        }
        fitSystemBars(root)
        setContentView(root)
    }

    // Android 15+ forces edge-to-edge for targetSdk 35, so content would draw under the
    // status bar, gesture bar and keyboard. Opt in on API 30+ everywhere for one code path,
    // and inset the root by exactly those areas. API 26–29 keep the legacy fitted window.
    private fun fitSystemBars(root: View) {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.R) return
        window.setDecorFitsSystemWindows(false)
        root.setOnApplyWindowInsetsListener { v, insets ->
            val bars = insets.getInsets(
                WindowInsets.Type.systemBars() or WindowInsets.Type.displayCutout() or WindowInsets.Type.ime(),
            )
            v.setPadding(bars.left, bars.top, bars.right, bars.bottom)
            WindowInsets.CONSUMED
        }
    }

    companion object {
        private const val TAILFWD = "io.lynch.tailfwd"
        private const val KEY_PORT = "port"
        private const val DEFAULT_PORT = 31510 // tailfwd's default lynch forward
    }
}
