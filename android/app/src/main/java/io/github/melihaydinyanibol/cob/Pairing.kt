package io.github.melihaydinyanibol.cob

import android.content.Context
import android.net.Uri

/** The PC this phone is paired with, as stored on the phone. */
data class PairedPc(
    val host: String,
    val port: Int,
    /** SHA-256 of the PC's TLS certificate, lowercase hex. Pinned on every connection. */
    val fingerprint: String,
    val name: String,
    /** One-time token from the QR code; set until pairing completes. */
    val pairingToken: String? = null,
    val deviceId: String? = null,
    val secret: String? = null,
) {
    val isPaired: Boolean get() = deviceId != null && secret != null

    companion object {
        /** Parses `cob://pair?h=<host>&p=<port>&f=<fingerprint>&t=<token>&n=<name>`. */
        fun fromPairingUri(text: String): PairedPc? {
            val uri = Uri.parse(text.trim())
            if (uri.scheme != "cob" || uri.host != "pair") return null
            val host = uri.getQueryParameter("h") ?: return null
            val port = uri.getQueryParameter("p")?.toIntOrNull() ?: return null
            val fingerprint = uri.getQueryParameter("f")?.lowercase() ?: return null
            val token = uri.getQueryParameter("t") ?: return null
            if (!Regex("[0-9a-f]{64}").matches(fingerprint) || port !in 1..65535) return null
            val name = uri.getQueryParameter("n") ?: host
            return PairedPc(host, port, fingerprint, name, pairingToken = token)
        }
    }
}

/** Keeps the pairing in app-private storage (excluded from backups). */
class PairingStore(context: Context) {
    private val prefs = context.getSharedPreferences("pairing", Context.MODE_PRIVATE)

    fun load(): PairedPc? {
        val host = prefs.getString("host", null) ?: return null
        val fingerprint = prefs.getString("fingerprint", null) ?: return null
        return PairedPc(
            host = host,
            port = prefs.getInt("port", 7480),
            fingerprint = fingerprint,
            name = prefs.getString("name", host) ?: host,
            pairingToken = prefs.getString("token", null),
            deviceId = prefs.getString("device_id", null),
            secret = prefs.getString("secret", null),
        )
    }

    fun save(pc: PairedPc) {
        prefs.edit()
            .putString("host", pc.host)
            .putInt("port", pc.port)
            .putString("fingerprint", pc.fingerprint)
            .putString("name", pc.name)
            .putString("token", pc.pairingToken)
            .putString("device_id", pc.deviceId)
            .putString("secret", pc.secret)
            .apply()
    }

    fun clear() {
        prefs.edit().clear().apply()
    }

    var enabled: Boolean
        get() = prefs.getBoolean("enabled", false)
        set(value) {
            prefs.edit().putBoolean("enabled", value).apply()
        }
}
