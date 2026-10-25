# COB Companion (Android)

The Android side of COB's Wi-Fi mode. It keeps an encrypted connection to
COB on your PC: it reports incoming calls and caller ID, carries out the
PC's answer / reject / hang up / dial commands, and sends battery, signal
and operator status.

Android 9 or newer. Android doesn't let apps carry call audio, so the
conversation stays on the phone unless the PC is also connected over
Bluetooth.

## Build and install

**Android Studio:** *File → Open* this `android` folder, wait for Gradle to
sync, connect the phone with USB debugging on, and press *Run*.

**Command line** (needs the Android SDK, e.g. from Android Studio):

```bash
cd android
./gradlew assembleDebug
adb install app/build/outputs/apk/debug/app-debug.apk
```

**Without building:** the repository's *Actions → Android app* workflow
builds the APK on every change. Download the `cob-companion-debug-apk`
artifact, copy the APK to the phone, and open it. Android asks you to allow
installing apps from that source.

## Use

1. On the PC, run `cob --wifi` and type `pair`.
2. In the app, tap **Pair with PC (scan QR code)** and scan the code, or
   copy the `cob://pair?...` link to the phone and use **Pair with a pasted
   link**.
3. Allow the permissions:

   | Permission | Why |
   |---|---|
   | Phone | see call state, answer, hang up, place calls |
   | Call logs | Android only reveals the caller's number with this |
   | Notifications | the "connected to PC" notification that keeps the link alive |
   | Camera | scanning the pairing QR code |

4. Tap **Keep running in the background** and turn off battery optimisation
   for COB Companion, so the link survives with the screen off.

The link starts again by itself after a reboot.

## How it works

| File | Role |
|---|---|
| `MainActivity.kt` | the single screen: pairing, start/stop, permissions |
| `CobService.kt` | foreground service: connect, handshake, run commands, reconnect |
| `SecureLink.kt` | TLS socket pinned to the PC's certificate fingerprint; UDP discovery |
| `PhoneControl.kt` | Android telephony: call state, answer, end, place call, status |
| `Pairing.kt` | the `cob://pair` link and the stored pairing |

The protocol (one JSON object per line over TLS) is documented in
`cob/wifi.py` and exercised by `tests/test_wifi.py` in the main project.
