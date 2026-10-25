package io.github.melihaydinyanibol.cob

import android.os.Handler
import android.os.Looper

/** The link's state, for the screen and the notification. Listeners run on the main thread. */
object LinkStatus {
    private val main = Handler(Looper.getMainLooper())
    private val listeners = mutableSetOf<(String) -> Unit>()

    @Volatile var text: String = "Not running"
        private set

    fun update(value: String) {
        text = value
        main.post { listeners.toList().forEach { it(value) } }
    }

    fun observe(listener: (String) -> Unit) {
        listeners += listener
        listener(text)
    }

    fun remove(listener: (String) -> Unit) {
        listeners -= listener
    }
}
