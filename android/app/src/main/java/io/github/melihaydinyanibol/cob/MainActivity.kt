package io.github.melihaydinyanibol.cob

import android.Manifest
import android.app.AlertDialog
import android.content.ClipboardManager
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.provider.Settings
import android.text.InputType
import android.view.View
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import android.widget.Toast
import androidx.activity.ComponentActivity
import androidx.activity.result.contract.ActivityResultContracts
import com.journeyapps.barcodescanner.ScanContract
import com.journeyapps.barcodescanner.ScanOptions

/** One screen: pair with the PC, turn the link on or off, grant permissions. */
class MainActivity : ComponentActivity() {
    private lateinit var store: PairingStore
    private lateinit var statusView: TextView
    private lateinit var pcView: TextView
    private lateinit var toggleButton: Button
    private lateinit var forgetButton: Button
    private val statusListener: (String) -> Unit = { statusView.text = it }

    private val phonePermissions = buildList {
        add(Manifest.permission.READ_PHONE_STATE)
        add(Manifest.permission.READ_CALL_LOG)
        add(Manifest.permission.ANSWER_PHONE_CALLS)
        add(Manifest.permission.CALL_PHONE)
        if (Build.VERSION.SDK_INT >= 33) add(Manifest.permission.POST_NOTIFICATIONS)
    }.toTypedArray()

    private val requestPermissions =
        registerForActivityResult(ActivityResultContracts.RequestMultiplePermissions()) {
            if (store.enabled) CobService.start(this) // pick up newly granted permissions
            refresh()
        }

    private val scanQr = registerForActivityResult(ScanContract()) { result ->
        result.contents?.let(::startPairing)
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        store = PairingStore(this)
        setContentView(buildLayout())
        if (missingPermissions().isNotEmpty()) requestPermissions.launch(phonePermissions)
        handlePairingIntent(intent)
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        handlePairingIntent(intent)
    }

    override fun onStart() {
        super.onStart()
        LinkStatus.observe(statusListener)
        refresh()
    }

    override fun onStop() {
        LinkStatus.remove(statusListener)
        super.onStop()
    }

    private fun handlePairingIntent(intent: Intent?) {
        val data = intent?.data ?: return
        if (data.scheme == "cob") startPairing(data.toString())
    }

    private fun startPairing(text: String) {
        val pc = PairedPc.fromPairingUri(text)
        if (pc == null) {
            Toast.makeText(this, "That is not a COB pairing code.", Toast.LENGTH_LONG).show()
            return
        }
        store.save(pc)
        store.enabled = true
        CobService.stop(this)
        CobService.start(this)
        refresh()
    }

    private fun missingPermissions() = phonePermissions.filter {
        checkSelfPermission(it) != PackageManager.PERMISSION_GRANTED
    }

    private fun refresh() {
        val pc = store.load()
        pcView.text = when {
            pc == null -> "Not paired with a PC yet."
            pc.isPaired -> "Paired with ${pc.name} (${pc.host}:${pc.port})"
            else -> "Pairing with ${pc.name}…"
        }
        toggleButton.visibility = if (pc != null) View.VISIBLE else View.GONE
        forgetButton.visibility = if (pc != null) View.VISIBLE else View.GONE
        toggleButton.text = if (store.enabled) "Stop link" else "Start link"
        val missing = missingPermissions()
        if (missing.isNotEmpty() && !store.enabled) {
            statusView.text = "Permissions needed: " + missing.joinToString { it.substringAfterLast('.') }
        }
    }

    private fun buildLayout(): View {
        val padding = (16 * resources.displayMetrics.density).toInt()
        val column = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(padding, padding, padding, padding)
        }
        fun text(size: Float) = TextView(this).apply {
            textSize = size
            setPadding(0, padding / 2, 0, padding / 2)
        }.also(column::addView)
        fun button(label: String, onClick: () -> Unit) = Button(this).apply {
            text = label
            setOnClickListener { onClick() }
        }.also(column::addView)

        text(22f).text = "COB Companion"
        text(14f).text = "Answer, reject and make calls from your PC over Wi-Fi. " +
            "Call audio uses Bluetooth when the PC is connected that way; otherwise you talk on the phone."
        pcView = text(16f)
        statusView = text(16f)

        button("Pair with PC (scan QR code)") {
            scanQr.launch(
                ScanOptions()
                    .setDesiredBarcodeFormats(ScanOptions.QR_CODE)
                    .setPrompt("Scan the code shown by 'pair' in COB on the PC")
                    .setBeepEnabled(false)
                    .setOrientationLocked(false),
            )
        }
        button("Pair with a pasted link") { askForPairingLink() }
        toggleButton = button("Start link") {
            store.enabled = !store.enabled
            if (store.enabled) CobService.start(this) else CobService.stop(this)
            refresh()
        }
        button("Grant phone permissions") {
            if (missingPermissions().isEmpty()) {
                Toast.makeText(this, "All permissions granted.", Toast.LENGTH_SHORT).show()
            } else {
                requestPermissions.launch(phonePermissions)
            }
        }
        button("Keep running in the background") {
            // Battery optimisation can cut the link while the screen is off.
            startActivity(Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS))
        }
        forgetButton = button("Forget this PC") {
            AlertDialog.Builder(this)
                .setMessage("Forget the paired PC? You will need to pair again.")
                .setPositiveButton("Forget") { _, _ ->
                    store.enabled = false
                    CobService.stop(this)
                    store.clear()
                    LinkStatus.update("Not paired")
                    refresh()
                }
                .setNegativeButton("Cancel", null)
                .show()
        }
        return ScrollView(this).apply { addView(column) }
    }

    private fun askForPairingLink() {
        val input = EditText(this).apply {
            hint = "cob://pair?..."
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_URI
            val clipboard = getSystemService(ClipboardManager::class.java)
            clipboard.primaryClip?.getItemAt(0)?.text?.toString()
                ?.takeIf { it.startsWith("cob://") }?.let(::setText)
        }
        AlertDialog.Builder(this)
            .setTitle("Pairing link")
            .setView(input)
            .setPositiveButton("Pair") { _, _ -> startPairing(input.text.toString()) }
            .setNegativeButton("Cancel", null)
            .show()
    }
}
