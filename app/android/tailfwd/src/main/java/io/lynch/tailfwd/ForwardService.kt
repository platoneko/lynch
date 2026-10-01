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
import java.util.concurrent.Executors
import tsnetlib.Tsnetlib

/**
 * Owns the forward: the tsnet node, the loopback port and the interface feed live
 * here, so the forward keeps running while Orca is in front or the screen is off.
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
        goForeground(notification(null))
        when {
            !running -> startForwarding()
            intent?.action == ACTION_RESTART -> {
                worker.execute { Tsnetlib.stop() }
                startNode()
            }
        }
        return START_STICKY
    }

    override fun onDestroy() {
        shutDownNode()
        super.onDestroy()
    }

    // ── Step 1: start / stop; every Tsnetlib start/stop runs in order on one worker ──

    private fun startForwarding() {
        running = true
        startError = null
        interfaces.watch()
        startNode()
        main.post(::poll)
    }

    private fun startNode() {
        val settings = Settings(this)
        val target = settings.target
        val port = settings.port
        val dir = File(filesDir, "tailnet").absolutePath
        worker.execute {
            val err = runCatching {
                interfaces.pushNow()
                Tsnetlib.startForward(dir, HOSTNAME, target, port.toLong())
            }.exceptionOrNull() ?: return@execute
            main.post {
                startError = err.message ?: err.toString()
                stopForwarding()
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
        main.removeCallbacksAndMessages(null)
        interfaces.unwatch()
        worker.execute { Tsnetlib.stop() }
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
        val headline = status?.headline(Settings(this).target) ?: "连接中…"
        val details = listOfNotNull(headline, status?.dialFailure()).joinToString("\n")
        val open = PendingIntent.getActivity(
            this, 0, Intent(this, MainActivity::class.java), PendingIntent.FLAG_IMMUTABLE,
        )
        val stop = PendingIntent.getService(
            this, 1, Intent(this, ForwardService::class.java).setAction(ACTION_STOP), PendingIntent.FLAG_IMMUTABLE,
        )
        return Notification.Builder(this, CHANNEL)
            .setSmallIcon(R.drawable.ic_forward)
            .setContentTitle("tailfwd")
            .setContentText(headline)
            .setStyle(Notification.BigTextStyle().bigText(details))
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .setContentIntent(open)
            .addAction(Notification.Action.Builder(Icon.createWithResource(this, R.drawable.ic_forward), "停止", stop).build())
            .build()
    }

    companion object {
        private const val ACTION_STOP = "io.lynch.tailfwd.STOP"
        private const val ACTION_RESTART = "io.lynch.tailfwd.RESTART"
        private const val HOSTNAME = "tailfwd-phone"
        private const val CHANNEL = "forward"
        private const val NOTIFICATION_ID = 1

        // One thread for all Tsnetlib start/stop calls, so a restart's Stop always lands before its Start.
        private val worker = Executors.newSingleThreadExecutor()

        /** Whether the user has the forward switched on; the screen and the tile read it. */
        @Volatile
        var running = false
            private set

        /** Why the last start failed (e.g. the port is taken); cleared by the next start. */
        @Volatile
        var startError: String? = null
            private set

        fun start(context: Context) {
            context.startForegroundService(Intent(context, ForwardService::class.java))
        }

        fun stop(context: Context) {
            context.startService(Intent(context, ForwardService::class.java).setAction(ACTION_STOP))
        }

        fun restart(context: Context) {
            context.startService(Intent(context, ForwardService::class.java).setAction(ACTION_RESTART))
        }
    }
}
