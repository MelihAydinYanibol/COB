package io.github.melihaydinyanibol.cob

import android.os.SystemClock
import org.json.JSONObject
import java.io.BufferedReader
import java.io.InputStreamReader
import java.io.OutputStream
import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.InetAddress
import java.net.InetSocketAddress
import java.net.Socket
import java.net.SocketTimeoutException
import java.security.MessageDigest
import java.security.SecureRandom
import java.security.cert.CertificateException
import java.security.cert.X509Certificate
import javax.net.ssl.SSLContext
import javax.net.ssl.SSLSocket
import javax.net.ssl.X509TrustManager

/**
 * Trusts exactly one certificate: the PC's, identified by the SHA-256
 * fingerprint from the pairing QR code. Anything else fails the handshake,
 * so nobody on the network can read or impersonate the link.
 */
class PinnedTrustManager(fingerprintHex: String) : X509TrustManager {
    private val expected = hexToBytes(fingerprintHex)

    override fun checkServerTrusted(chain: Array<out X509Certificate>?, authType: String?) {
        val leaf = chain?.firstOrNull() ?: throw CertificateException("no certificate")
        val actual = MessageDigest.getInstance("SHA-256").digest(leaf.encoded)
        if (!MessageDigest.isEqual(actual, expected)) {
            throw CertificateException("certificate does not match the paired PC")
        }
    }

    override fun checkClientTrusted(chain: Array<out X509Certificate>?, authType: String?) {
        throw CertificateException("not a server")
    }

    override fun getAcceptedIssuers(): Array<X509Certificate> = emptyArray()

    private companion object {
        fun hexToBytes(hex: String): ByteArray =
            ByteArray(hex.length / 2) { i -> hex.substring(2 * i, 2 * i + 2).toInt(16).toByte() }
    }
}

/** One TLS connection to COB, exchanging one JSON object per line. */
class SecureLink private constructor(private val socket: SSLSocket) {
    private val reader = BufferedReader(InputStreamReader(socket.inputStream, Charsets.UTF_8))
    private val output: OutputStream = socket.outputStream

    /** Blocks for the next message; null when the PC closed the connection. */
    fun receive(): JSONObject? {
        val line = reader.readLine() ?: return null
        return JSONObject(line)
    }

    @Synchronized
    fun send(message: JSONObject) {
        output.write((message.toString() + "\n").toByteArray(Charsets.UTF_8))
        output.flush()
    }

    fun close() {
        try {
            socket.close()
        } catch (_: Exception) {
        }
    }

    companion object {
        private const val CONNECT_TIMEOUT_MS = 5_000

        /** The PC answers our pings every 25 s, so 90 s of silence means the link is dead. */
        private const val READ_TIMEOUT_MS = 90_000

        fun open(host: String, port: Int, fingerprint: String): SecureLink {
            val context = SSLContext.getInstance("TLS")
            context.init(null, arrayOf(PinnedTrustManager(fingerprint)), SecureRandom())
            val raw = Socket()
            try {
                raw.connect(InetSocketAddress(host, port), CONNECT_TIMEOUT_MS)
                raw.soTimeout = READ_TIMEOUT_MS
                val tls = context.socketFactory.createSocket(raw, host, port, true) as SSLSocket
                tls.startHandshake()
                return SecureLink(tls)
            } catch (e: Exception) {
                raw.close()
                throw e
            }
        }

        /**
         * Finds the paired PC on the local network when its address changed.
         * COB answers a UDP broadcast with its port and certificate fingerprint prefix.
         */
        fun discover(pc: PairedPc, timeoutMs: Long = 2_000): Pair<String, Int>? {
            val request = "COB-DISCOVER".toByteArray()
            DatagramSocket().use { socket ->
                socket.broadcast = true
                socket.soTimeout = 300
                val broadcast = InetAddress.getByName("255.255.255.255")
                socket.send(DatagramPacket(request, request.size, broadcast, pc.port))
                val buffer = ByteArray(1024)
                val deadline = SystemClock.elapsedRealtime() + timeoutMs
                while (SystemClock.elapsedRealtime() < deadline) {
                    val packet = DatagramPacket(buffer, buffer.size)
                    try {
                        socket.receive(packet)
                    } catch (_: SocketTimeoutException) {
                        continue
                    }
                    val reply = try {
                        JSONObject(String(packet.data, 0, packet.length, Charsets.UTF_8))
                    } catch (_: Exception) {
                        continue
                    }
                    val fingerprintPrefix = reply.optString("fp")
                    if (fingerprintPrefix.length >= 16 && pc.fingerprint.startsWith(fingerprintPrefix)) {
                        val address = packet.address.hostAddress ?: continue
                        return address to reply.optInt("port", pc.port)
                    }
                }
            }
            return null
        }
    }
}
