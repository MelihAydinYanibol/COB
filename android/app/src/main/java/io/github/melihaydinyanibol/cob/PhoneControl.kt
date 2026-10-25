package io.github.melihaydinyanibol.cob

import android.Manifest
import android.annotation.SuppressLint
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.PackageManager
import android.net.Uri
import android.os.BatteryManager
import android.os.Bundle
import android.telecom.TelecomManager
import android.telephony.PhoneStateListener
import android.telephony.TelephonyManager
import org.json.JSONObject

/**
 * What a regular Android app may do with phone calls: watch the call state,
 * answer, reject or end calls, and place new ones. Android gives apps no
 * access to call audio, so the conversation stays on the phone (or on COB's
 * Bluetooth link).
 */
class PhoneControl(private val context: Context) {
    private val telephony = context.getSystemService(TelephonyManager::class.java)
    private val telecom = context.getSystemService(TelecomManager::class.java)
    private var listener: PhoneStateListener? = null

    /** Last known state: "idle", "ringing" or "offhook", and the other party's number. */
    @Volatile var state: String = "idle"
        private set
    @Volatile var number: String? = null
        private set

    class MissingPermission(permission: String) :
        Exception("permission ${permission.substringAfterLast('.')} not granted on the phone")

    private fun require(permission: String) {
        if (context.checkSelfPermission(permission) != PackageManager.PERMISSION_GRANTED) {
            throw MissingPermission(permission)
        }
    }

    /**
     * Reports call state changes on the main thread. The legacy listener is
     * used because it is the only one that passes the caller's number.
     */
    @Suppress("DEPRECATION")
    fun startWatching(onChange: (state: String, number: String?) -> Unit) {
        if (listener != null) return
        if (context.checkSelfPermission(Manifest.permission.READ_PHONE_STATE) !=
            PackageManager.PERMISSION_GRANTED
        ) return
        val callListener = object : PhoneStateListener() {
            @Deprecated("Deprecated in Java")
            override fun onCallStateChanged(callState: Int, phoneNumber: String?) {
                val newState = when (callState) {
                    TelephonyManager.CALL_STATE_RINGING -> "ringing"
                    TelephonyManager.CALL_STATE_OFFHOOK -> "offhook"
                    else -> "idle"
                }
                val reported = phoneNumber?.takeIf { it.isNotEmpty() }
                when (newState) {
                    "ringing" -> number = reported // a new call: drop any old number
                    "offhook" -> if (reported != null) number = reported // keep a number dialed from the PC
                }
                state = newState
                onChange(newState, number)
                if (newState == "idle") number = null
            }
        }
        telephony.listen(callListener, PhoneStateListener.LISTEN_CALL_STATE)
        listener = callListener
    }

    @Suppress("DEPRECATION")
    fun stopWatching() {
        listener?.let { telephony.listen(it, PhoneStateListener.LISTEN_NONE) }
        listener = null
    }

    @SuppressLint("MissingPermission")
    @Suppress("DEPRECATION")
    fun answer() {
        require(Manifest.permission.ANSWER_PHONE_CALLS)
        telecom.acceptRingingCall()
    }

    @SuppressLint("MissingPermission")
    @Suppress("DEPRECATION")
    fun hangUp() {
        require(Manifest.permission.ANSWER_PHONE_CALLS)
        if (!telecom.endCall()) throw IllegalStateException("no call to end")
    }

    @SuppressLint("MissingPermission")
    fun dial(target: String) {
        require(Manifest.permission.CALL_PHONE)
        if (!Regex("[0-9*#+,;A-Da-d]+").matches(target)) throw IllegalArgumentException("bad number")
        number = target
        telecom.placeCall(Uri.fromParts("tel", target, null), Bundle())
    }

    /** Battery, signal and operator, as sent to the PC. */
    fun status(): JSONObject {
        val result = JSONObject().put("type", "status")
        val battery = context.registerReceiver(null, IntentFilter(Intent.ACTION_BATTERY_CHANGED))
        if (battery != null) {
            val level = battery.getIntExtra(BatteryManager.EXTRA_LEVEL, -1)
            val scale = battery.getIntExtra(BatteryManager.EXTRA_SCALE, 100)
            if (level >= 0 && scale > 0) result.put("battery", level * 100 / scale)
            val plugged = battery.getIntExtra(BatteryManager.EXTRA_PLUGGED, 0)
            result.put("charging", plugged != 0)
        }
        telephony.signalStrength?.let { result.put("signal", it.level) }
        telephony.networkOperatorName?.takeIf { it.isNotEmpty() }?.let { result.put("operator", it) }
        return result
    }
}
