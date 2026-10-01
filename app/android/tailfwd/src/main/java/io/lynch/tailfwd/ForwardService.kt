package io.lynch.tailfwd

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.graphics.drawable.Icon
import android.os.Build
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import io.lynch.tailnet.InterfaceFeed
import java.io.File
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.Executors
import tsnetlib.Tsnetlib

/**
 * Owns the node and its forwards: the tsnet node, the loopback ports and the interface feed live
 * here, so forwarding keeps running while another app is in front or the screen is off.
 */
class ForwardService : Service() {
    private val main = Handler(Looper.getMainLooper())
    private lateinit var interfaces: InterfaceFeed

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        interfaces = InterfaceFeed(this) { Tsnetlib.setInterfaces(it) }
        getSystemService(NotificationManager::class.java).createNotificationChannel(
            NotificationChannel(CHANNEL, "转发状态", NotificationManager.IMPORTANCE_LOW),
        )
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == ACTION_STOP) {
            stopForwarding()
            return START_NOT_STICKY
        }
        // A null intent is the system re-creating us after a kill (START_STICKY): forward again.
        try {
            goForeground(notification(null))
        } catch (e: IllegalStateException) {
            // Android 12+ forbids entering the foreground from the background
            // (ForegroundServiceStartNotAllowedException); the system's sticky restart after a
            // kill, or any start while the app is in the background, can hit it. Ask the user to start it.
            notifyFailure("无法在后台启动转发，请打开 tailfwd 启动")
            stopSelf()
            return START_NOT_STICKY
        }
        if (!running) startForwarding()
        return START_STICKY
    }

    override fun onDestroy() {
        shutDownNode()
        super.onDestroy()
    }

    // ── Step 1: the master switch; every Tsnetlib call runs in order on one worker ──

    private fun startForwarding() {
        running = true
        startError = null
        getSystemService(NotificationManager::class.java).cancel(FAILURE_NOTIFICATION_ID)
        interfaces.watch()
        startNode()
        main.post(::poll)
    }

    private fun startNode() {
        val settings = Settings(this)
        val hostname = settings.hostname
        val wanted = settings.forwards.filter { it.enabled }
        val dir = File(filesDir, "tailnet").absolutePath
        val gen = ++generation
        worker.execute {
            val err = runCatching {
                interfaces.pushNow()
                Tsnetlib.startNode(dir, hostname)
            }.exceptionOrNull()
            if (err == null) {
                nodeUp = true
                reconcile(wanted)
                return@execute
            }
            main.post {
                // A queued start can fail after a later start or a stop, possibly of another
                // service instance; only the current start's failure may stop the service.
                if (gen != generation) return@post
                val reason = err.message ?: err.toString()
                startError = reason
                stopForwarding()
                notifyFailure("启动失败：$reason")
            }
        }
    }

    private fun stopForwarding() {
        shutDownNode()
        stopForeground(STOP_FOREGROUND_REMOVE)
        stopSelf()
    }

    private fun shutDownNode() {
        if (!running) return
        running = false
        generation++
        main.removeCallbacksAndMessages(null)
        interfaces.unwatch()
        worker.execute {
            Tsnetlib.stop()
            nodeUp = false
            opened.clear()
            errors.clear()
        }
    }

    // ── Step 2: keep the notification in step with the node ──

    private fun poll() {
        getSystemService(NotificationManager::class.java)
            .notify(NOTIFICATION_ID, notification(ForwardStatus.parse(Tsnetlib.status())))
        main.postDelayed(::poll, 1000)
    }

    private fun goForeground(n: Notification) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE) {
            startForeground(NOTIFICATION_ID, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE)
        } else {
            startForeground(NOTIFICATION_ID, n)
        }
    }

    private fun notification(status: ForwardStatus?): Notification {
        val headline = status?.headline() ?: "连接中…"
        val problems = status?.let { s ->
            Settings(this).forwards.filter { it.enabled }.flatMap { f ->
                s.problems(f, errors[f.id]).map { "${f.name}：$it" }
            }
        }.orEmpty()
        val stop = PendingIntent.getService(
            this, 1, Intent(this, ForwardService::class.java).setAction(ACTION_STOP), PendingIntent.FLAG_IMMUTABLE,
        )
        return Notification.Builder(this, CHANNEL)
            .setSmallIcon(R.drawable.ic_forward)
            .setContentTitle("tailfwd · ${status?.openCount ?: 0} 条转发中")
            .setContentText(headline)
            .setStyle(Notification.BigTextStyle().bigText((listOf(headline) + problems).joinToString("\n")))
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .setContentIntent(openScreen())
            .addAction(Notification.Action.Builder(Icon.createWithResource(this, R.drawable.ic_forward), "停止", stop).build())
            .build()
    }

    // Dismissible, and a tap opens the screen; the next start cancels it.
    private fun notifyFailure(text: String) {
        val n = Notification.Builder(this, CHANNEL)
            .setSmallIcon(R.drawable.ic_forward)
            .setContentTitle("tailfwd")
            .setContentText(text)
            .setStyle(Notification.BigTextStyle().bigText(text))
            .setAutoCancel(true)
            .setContentIntent(openScreen())
            .build()
        getSystemService(NotificationManager::class.java).notify(FAILURE_NOTIFICATION_ID, n)
    }

    private fun openScreen(): PendingIntent = PendingIntent.getActivity(
        this, 0, Intent(this, MainActivity::class.java), PendingIntent.FLAG_IMMUTABLE,
    )

    companion object {
        private const val ACTION_STOP = "io.lynch.tailfwd.STOP"
        private const val CHANNEL = "forward"
        private const val NOTIFICATION_ID = 1
        // Not NOTIFICATION_ID: stopForeground's asynchronous cancel of that id could remove a
        // failure notification posted right after it. The next start cancels this one instead.
        private const val FAILURE_NOTIFICATION_ID = 2

        // One thread for all Tsnetlib start / open / close / stop calls, so they land in order.
        private val worker = Executors.newSingleThreadExecutor()

        // Bumped by every start and stop (main thread only); a start's failure counts only while
        // it is still current. This also keeps watch() / unwatch() paired: only the live instance
        // with running == true ever reaches stopForwarding() from a failure.
        private var generation = 0

        // Worker thread only: whether StartNode succeeded, and each open forward as it was opened.
        private var nodeUp = false
        private val opened = mutableMapOf<String, Forward>()

        private val errors = ConcurrentHashMap<String, String>()

        /** Why a forward failed to open (e.g. its port is taken), by id; its next open replaces it. */
        val openErrors: Map<String, String> get() = errors

        /** Whether the user has the master switch on; the screen reads it. */
        @Volatile
        var running = false
            private set

        /** Why the last node start failed; cleared by the next start. */
        @Volatile
        var startError: String? = null
            private set

        fun start(context: Context) {
            context.startForegroundService(Intent(context, ForwardService::class.java))
        }

        fun stop(context: Context) {
            context.startService(Intent(context, ForwardService::class.java).setAction(ACTION_STOP))
        }

        /**
         * Applies the saved list to the running node at once: opens what was added or switched
         * on, closes what was deleted or switched off, reopens what changed port or target, and
         * leaves every other forward alone. While stopped the list just waits for the next start.
         */
        fun sync(context: Context) {
            if (!running) return
            val wanted = Settings(context).forwards.filter { it.enabled }
            worker.execute { reconcile(wanted) }
        }

        // Worker thread.
        private fun reconcile(wanted: List<Forward>) {
            if (!nodeUp) return // the start failed, or a stop is queued ahead of this
            val plan = Forwards.plan(opened, wanted)
            // Closes first: a port may be moving from a closed forward to an opened one.
            for (id in plan.close) {
                Tsnetlib.closeForward(id)
                opened.remove(id)
            }
            // A deleted or switched-off forward's old failure is no longer news.
            errors.keys.retainAll(wanted.map { it.id }.toSet())
            for (f in plan.open) {
                runCatching { Tsnetlib.openForward(f.id, f.port.toLong(), f.target) }
                    .onSuccess {
                        opened[f.id] = f
                        errors.remove(f.id)
                    }
                    .onFailure { errors[f.id] = it.message ?: it.toString() }
            }
        }
    }
}
