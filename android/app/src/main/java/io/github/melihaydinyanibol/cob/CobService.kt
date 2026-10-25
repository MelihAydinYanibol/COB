package io.github.melihaydinyanibol.cob

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import android.provider.Settings
import android.util.Log
import androidx.core.app.ServiceCompat
import org.json.JSONObject
import java.util.concurrent.Executors
import java.util.concurrent.ScheduledExecutorService
import java.util.concurrent.TimeUnit

/**
 * Keeps an encrypted connection to the paired PC: reports calls and phone
 * status, and carries out the PC's commands. Reconnects on its own and finds
 * the PC again if its address changes.
 */
class CobService : Service() {
    private lateinit var store: PairingStore
    private lateinit var phone: PhoneControl
    private lateinit var scheduler: ScheduledExecutorService
    private val sender = Executors.newSingleThreadExecutor()

    @Volatile private var running = false
    @Volatile private var link: SecureLink? = null
    private var worker: Thread? = null

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        store = PairingStore(this)
        phone = PhoneControl(this)
        scheduler = Executors.newSingleThreadScheduledExecutor()
        createChannel()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        ServiceCompat.startForeground(
            this, NOTIFICATION_ID, notification("Starting…"),
            ServiceInfo.FOREGROUND_SERVICE_TYPE_CONNECTED_DEVICE,
        )
        // Again on every start: permissions may have been granted since.
        phone.startWatching { state, number ->
            send(JSONObject().put("type", "call").put("state", state).put("number", number ?: JSONObject.NULL))
        }
        if (!running) {
            running = true
            scheduler.scheduleWithFixedDelay({ send(JSONObject().put("type", "ping")) }, 25, 25, TimeUnit.SECONDS)
            scheduler.scheduleWithFixedDelay({ send(phone.status()) }, 60, 60, TimeUnit.SECONDS)
            worker = Thread(::connectionLoop, "cob-link").also { it.start() }
        } else {
            link?.close() // e.g. new pairing: reconnect with it
        }
        return START_STICKY
    }

    override fun onDestroy() {
        val wasRunning = running // false when stopLink already explained why
        running = false
        phone.stopWatching()
        scheduler.shutdownNow()
        link?.close()
        worker?.interrupt()
        sender.shutdownNow()
        if (wasRunning) LinkStatus.update("Stopped")
        super.onDestroy()
    }

    // -- connection --------------------------------------------------------

    private fun connectionLoop() {
        var delayMs = 2_000L
        while (running) {
            val pc = store.load()
            if (pc == null || (!pc.isPaired && pc.pairingToken == null)) {
                stopLink("Not paired. Run 'pair' on the PC and scan the code.")
                return
            }
            try {
                session(pc)
                delayMs = 2_000L
            } catch (e: InterruptedException) {
                return
            } catch (e: Refused) {
                if (!pc.isPaired) {
                    store.save(pc.copy(pairingToken = null)) // the token is spent either way
                    stopLink("Pairing failed: ${e.message}")
                    return
                }
                setStatus("${pc.name} refused the connection: ${e.message}")
                delayMs = 60_000L
            } catch (e: Exception) {
                Log.i(TAG, "link to ${pc.name} failed", e)
                setStatus("Looking for ${pc.name}… (${e.message ?: e.javaClass.simpleName})")
            } finally {
                link?.close()
                link = null
            }
            if (!running) return
            try {
                Thread.sleep(delayMs)
            } catch (_: InterruptedException) {
                return
            }
            delayMs = (delayMs * 2).coerceAtMost(30_000L)
        }
    }

    private class Refused(message: String) : Exception(message)

    private fun stopLink(text: String) {
        store.enabled = false
        running = false
        LinkStatus.update(text)
        stopSelf()
    }

    /** One connection, from handshake until it drops. */
    private fun session(stored: PairedPc) {
        var pc = stored
        setStatus("Connecting to ${pc.name}…")
        val opened = try {
            SecureLink.open(pc.host, pc.port, pc.fingerprint)
        } catch (e: java.io.IOException) {
            // The PC may have a new address (another machine may even hold the
            // old one). Discovery only finds hosts whose certificate matches,
            // and the pin is checked again when connecting.
            val found = SecureLink.discover(pc) ?: throw e
            pc = pc.copy(host = found.first, port = found.second)
            store.save(pc)
            SecureLink.open(pc.host, pc.port, pc.fingerprint)
        }
        link = opened

        val hello = JSONObject().put("name", deviceName())
        if (pc.isPaired) {
            hello.put("type", "hello").put("device_id", pc.deviceId).put("secret", pc.secret)
        } else {
            hello.put("type", "pair").put("token", pc.pairingToken)
        }
        opened.send(hello)
        val reply = opened.receive() ?: throw java.io.IOException("closed during handshake")
        when (reply.optString("type")) {
            "paired" -> {
                pc = pc.copy(
                    pairingToken = null,
                    deviceId = reply.getString("device_id"),
                    secret = reply.getString("secret"),
                    name = reply.optString("name", pc.name),
                )
                store.save(pc)
            }
            "welcome" -> Unit
            "error" -> throw Refused(reply.optString("text", "unknown error"))
            else -> throw java.io.IOException("unexpected reply")
        }

        setStatus("Connected to ${pc.name} over Wi-Fi")
        send(JSONObject().put("type", "call").put("state", phone.state).put("number", phone.number ?: JSONObject.NULL))
        send(phone.status())

        while (running) {
            val message = opened.receive() ?: break
            if (message.optString("type") == "command") handleCommand(message)
        }
    }

    private fun handleCommand(command: JSONObject) {
        val result = JSONObject().put("type", "result").put("id", command.opt("id"))
        try {
            when (command.optString("action")) {
                "answer" -> phone.answer()
                "hangup" -> phone.hangUp()
                "dial" -> phone.dial(command.getString("number"))
                "status" -> send(phone.status())
                else -> throw UnsupportedOperationException("unknown command")
            }
            result.put("ok", true)
        } catch (e: Exception) {
            result.put("ok", false).put("error", e.message ?: e.javaClass.simpleName)
        }
        send(result)
    }

    /** Sends from a background thread; the main thread may not touch the network. */
    private fun send(message: JSONObject) {
        val current = link ?: return
        try {
            sender.execute {
                try {
                    current.send(message)
                } catch (e: Exception) {
                    Log.i(TAG, "send failed", e)
                    current.close()
                }
            }
        } catch (_: java.util.concurrent.RejectedExecutionException) {
        }
    }

    private fun deviceName(): String =
        Settings.Global.getString(contentResolver, Settings.Global.DEVICE_NAME)
            ?: "${Build.MANUFACTURER} ${Build.MODEL}"

    // -- notification ------------------------------------------------------

    private fun setStatus(text: String) {
        LinkStatus.update(text)
        if (running) {
            getSystemService(NotificationManager::class.java).notify(NOTIFICATION_ID, notification(text))
        }
    }

    private fun createChannel() {
        val channel = NotificationChannel(CHANNEL_ID, "Link to PC", NotificationManager.IMPORTANCE_LOW)
        getSystemService(NotificationManager::class.java).createNotificationChannel(channel)
    }

    private fun notification(text: String): Notification {
        val open = PendingIntent.getActivity(
            this, 0, Intent(this, MainActivity::class.java), PendingIntent.FLAG_IMMUTABLE,
        )
        return Notification.Builder(this, CHANNEL_ID)
            .setSmallIcon(android.R.drawable.sym_action_call)
            .setContentTitle("COB")
            .setContentText(text)
            .setContentIntent(open)
            .setOngoing(true)
            .build()
    }

    companion object {
        private const val TAG = "CobService"
        private const val CHANNEL_ID = "link"
        private const val NOTIFICATION_ID = 1

        fun start(context: Context) {
            context.startForegroundService(Intent(context, CobService::class.java))
        }

        fun stop(context: Context) {
            context.stopService(Intent(context, CobService::class.java))
        }
    }
}
