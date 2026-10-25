package io.github.melihaydinyanibol.cob

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

/** Restarts the link after a reboot if it was on. */
class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action == Intent.ACTION_BOOT_COMPLETED && PairingStore(context).enabled) {
            CobService.start(context)
        }
    }
}
