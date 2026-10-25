# COB — CallOverBluetooth

COB turns your PC into a Bluetooth hands-free kit for your phone, like a car
kit or a Bluetooth headset. The phone pairs with the PC as a **Hands-Free
device (HFP)**, so you can make, answer and talk on phone calls with the PC's
microphone and speakers (or headset).

With the **COB Companion** Android app, COB also works over your **local
Wi-Fi**, encrypted, for when the phone is out of Bluetooth range or the PC
has no Bluetooth. See [Over Wi-Fi](#over-wi-fi).

Works on **Linux (Fedora and others)** and **Windows 10/11**.

```
* COB 0.1.0 ready on BlueZ (hci0).
* Connected to Pixel 8 (Turkcell).
* Incoming call from +905551112233. 'a' to answer, 'h' to reject.
a
* Call active with +905551112233.
* Call audio is on the PC.
h
* Call ended.
```

## Features

- Answer, reject, hang up, dial, redial
- Caller ID, call waiting, three-way calling (hold / swap / merge)
- Two-way call audio through the PC's microphone and speakers
- Move audio between the PC and the phone during a call
- DTMF key presses (menus, voicemail PINs)
- Mic mute, speaker volume synced with the phone's volume buttons
- Phone status: network, signal, battery, operator
- Starts the phone's voice assistant (Google Assistant / Siri)
- Plays a ringtone on the PC when the phone doesn't send its own
- Over Wi-Fi (Android): ring alerts, caller ID, answer, reject, hang up,
  dial, battery and signal, all TLS-encrypted

## How it works

```
 Phone (Audio Gateway)                         PC (Hands-Free)
 ─────────────────────                         ──────────────────────────────
 RFCOMM  ◄──── AT commands (ATA, +CIEV …) ───► cob.hfp    call control & state
 SCO     ◄──── 8 kHz CVSD voice ─────────────► cob.audio  mic / speakers
                         ▲
              cob.backends: BlueZ (Linux) or Bumble (Windows)

 COB Companion app ◄── TLS over Wi-Fi (JSON) ──► cob.wifi  call control & status
```

- `cob/hfp.py`: the Hands-Free protocol. It sets up the Service Level
  Connection, tracks the phone's indicators, works out the call state, and
  sends call control commands. It knows nothing about Bluetooth hardware.
- `cob/audio.py`: bridges the voice link to the PC's sound devices
  (PortAudio via `sounddevice`) and resamples between 8 kHz and the device rate.
- `cob/backends/bluez.py`: Linux. Registers COB with the system BlueZ daemon
  over D-Bus and uses kernel SCO sockets for audio.
- `cob/backends/bumble.py`: Windows. Windows doesn't let applications act as
  a hands-free device or touch voice (SCO) links, so COB brings its own
  Bluetooth stack ([Bumble](https://github.com/google/bumble)) and drives a USB
  Bluetooth adapter directly.
- `cob/wifi.py`: the encrypted Wi-Fi link to the Android companion app.
- `cob/cli.py`: the command line app. It uses Bluetooth when it's connected
  and falls back to Wi-Fi otherwise.
- `android/`: the COB Companion app (Kotlin).

## Installation

You need Python 3.10 or newer.

### Linux (Fedora)

```bash
sudo dnf install python3-pip portaudio bluez
git clone https://github.com/MelihAydinYanibol/COB.git
cd COB
python3 -m venv .venv && source .venv/bin/activate
pip install .
```

On Debian/Ubuntu, install `libportaudio2 bluez python3-venv` instead.

**Stop PipeWire from taking the hands-free role.** PipeWire (Fedora's default
sound server) registers the hands-free role itself, and only one program can
hold it. Tell WirePlumber to leave the hands-free and headset roles alone:

```bash
mkdir -p ~/.config/wireplumber/wireplumber.conf.d
cat > ~/.config/wireplumber/wireplumber.conf.d/80-cob.conf <<'EOF'
monitor.bluez.properties = {
  bluez5.roles = [ a2dp_sink a2dp_source bap_sink bap_source ]
}
EOF
systemctl --user restart wireplumber
```

Bluetooth music (A2DP) keeps working. Delete the file to undo it. If you run
PulseAudio or oFono instead, disable their HFP/HSP support (or stop `ofono`).

### Windows 10 / 11

Windows has no API for being a hands-free device, so COB needs **a USB
Bluetooth adapter of its own** (any cheap Bluetooth 4.0+ dongle). COB drives
it directly, and **Windows can't use that adapter while COB has it.** Keep
your built-in Bluetooth for your mouse, headphones and so on. A dongle with
Bluetooth 4.2 or newer (Realtek, Intel, Broadcom) works best for audio.

1. Install [Python](https://www.python.org/downloads/windows/) and tick
   "Add python.exe to PATH".
2. Plug in the dongle and give it the WinUSB driver with
   [Zadig](https://zadig.akeo.ie/): *Options → List All Devices*, pick the
   dongle (check the USB ID; **don't pick your built-in adapter**), choose
   **WinUSB** and click *Replace Driver*.
3. Install COB:

   ```powershell
   git clone https://github.com/MelihAydinYanibol/COB.git
   cd COB
   py -m venv .venv
   .venv\Scripts\activate
   pip install .
   ```

To give the dongle back to Windows: Device Manager → right-click it →
*Uninstall device* → tick *Delete the driver software* → unplug and replug it.

## Usage

### First time: pairing

**Linux:** pair the phone with the PC as usual, from GNOME/KDE Bluetooth
settings or `bluetoothctl`:

```bash
bluetoothctl
[bluetooth]# scan on          # wait for your phone to show up
[bluetooth]# pair AA:BB:CC:DD:EE:FF
[bluetooth]# trust AA:BB:CC:DD:EE:FF
```

**Windows:** start `cob`. On the phone, search for Bluetooth devices, pick
**COB Hands-Free** and accept the pairing.

### Running

```bash
cob                            # wait for the phone to connect
cob --device AA:BB:CC:DD:EE:FF # connect to a paired phone
```

On Android, make sure **Phone calls** is enabled for the PC in the phone's
Bluetooth settings. On iPhone, the PC shows up as a car kit or headset.

When a call starts, the phone moves the call audio to the PC. Type `x` to
move it back and forth.

### Commands

| Command | Action |
|---|---|
| `a` / `answer` | answer the incoming call |
| `h` / `hangup` | hang up, or reject an incoming call |
| `d <number>` / `dial` | call a number |
| `r` / `redial` | redial the last number |
| `t <digits>` / `dtmf` | send key presses during a call |
| `x` / `audio` | move call audio between PC and phone |
| `m` / `mute` | mute / unmute the PC microphone |
| `vol <0-15>` | speaker volume |
| `mic <0-15>` | microphone gain reported to the phone |
| `hold <n>` | three-way calling: `0` reject waiting call, `1` end current and take the other, `2` swap, `3` merge |
| `vr [on\|off]` | phone's voice assistant |
| `calls` | list current calls |
| `s` / `status` | phone and connection status |
| `connect <address>` | connect to a paired phone over Bluetooth |
| `at <command>` | send a raw AT command |
| `pair` | show a QR code to pair the Android app (Wi-Fi) |
| `devices` | list phones paired over Wi-Fi |
| `unpair <name\|id>` | forget a phone paired over Wi-Fi |
| `q` / `quit` | exit |

Over Wi-Fi, COB can answer, reject, hang up, dial, redial and show status.
The other commands need the Bluetooth connection.

### Options

| Option | Meaning |
|---|---|
| `--backend bluez\|bumble\|none` | Bluetooth stack (default: `bluez` on Linux, `bumble` on Windows; `none` for Wi-Fi only) |
| `--wifi` | accept the COB Companion app over the local network |
| `--wifi-port 7480` | Wi-Fi link port (TCP for the link, UDP for discovery) |
| `--wifi-bind 0.0.0.0` | network address to listen on |
| `--wifi-host ADDRESS` | address put in the pairing QR code, if COB guesses wrong |
| `-d, --device ADDRESS` | phone to connect to |
| `--adapter hci0` | BlueZ adapter to use |
| `--transport SPEC` | Bumble HCI transport (default `usb:0+sco=0`: first USB dongle, SCO audio on) |
| `--name NAME` | Bluetooth name shown to the phone (Bumble) |
| `--keystore FILE` | where to keep pairing keys (Bumble) |
| `--list-audio` | list sound devices |
| `--input-device`, `--output-device` | microphone / speakers, by index or name |
| `--audio-rate 48000` | sound device sample rate (a multiple of 8000) |
| `--no-ringtone` | don't play a ringtone on the PC |
| `-v`, `-vv` | more logging; `-vv` shows every AT command |

The Bumble backend also runs on Linux, which is useful with a dedicated
dongle (`--backend bumble --transport usb:0+sco=0`; give your user access to
the USB device, and keep BlueZ off that adapter).

## Over Wi-Fi

The **COB Companion** app for Android connects to COB over your local
network. Use it when the phone is out of Bluetooth range (charging in another
room) or the PC has no Bluetooth at all.

**What works over Wi-Fi:** ring alerts and ringtone on the PC, caller ID,
answer, reject, hang up, dial, redial, battery/signal/operator status.

**Call audio needs Bluetooth.** Since Android 10, regular apps can't record
call audio or feed a microphone into a call. Only system apps can. So when
only Wi-Fi is connected, you answer from the PC and talk on the phone
(speakerphone works well). When Bluetooth is connected too, COB uses it for
both control and audio; Wi-Fi then just stands by. iPhones allow none of
this, so the app is Android-only.

### Setup

1. **Install the app.** Build it from [`android/`](android/README.md) with
   Android Studio, or download the `cob-companion-debug-apk` artifact from the
   repository's *Actions → Android app* workflow. Then install the APK on the
   phone.
2. **Start COB with Wi-Fi on:**

   ```bash
   cob --wifi                  # Bluetooth and Wi-Fi
   cob --wifi --backend none   # Wi-Fi only (no Bluetooth needed)
   ```

3. **Open the firewall** for port 7480 (TCP and UDP):
   - Fedora: `sudo firewall-cmd --permanent --add-port=7480/tcp --add-port=7480/udp && sudo firewall-cmd --reload`
   - Windows: when Windows asks, allow Python on **private** networks.
4. **Pair:** type `pair` in COB. In the app, tap **Pair with PC (scan QR
   code)** and scan the code. Grant the phone permissions the app asks for.
   From then on the phone reconnects by itself, even after the PC's address
   changes. Type `devices` to see paired phones, `unpair <name>` to remove one.

### Security

- The link is **TLS-encrypted** end to end. COB creates its own certificate
  on first use, and the app accepts only that certificate: its SHA-256
  fingerprint is in the QR code and pinned in the app. Someone else on the
  Wi-Fi can't read the link or pretend to be your PC.
- The QR code holds a **one-time pairing token** that expires after 10
  minutes. Pairing trades it for a random per-phone secret. COB stores only a
  hash of that secret, and the app keeps it in private storage that's
  excluded from backups.
- The certificate, key and paired phones live in `~/.config/cob/` (Linux) or
  `%APPDATA%\COB\` (Windows). Delete `wifi-cert.pem` and `wifi-key.pem` to
  make a new certificate; every phone then has to pair again.
- Only use `--wifi` on networks you trust to be your own (home Wi-Fi). It
  isn't meant to be exposed to the internet.

## Limitations

- **Narrowband audio.** COB uses the CVSD codec (8 kHz, phone quality). The
  mSBC wideband codec isn't supported yet.
- **No echo cancellation.** Use a headset or headphones, or the other person
  will hear themselves through your speakers.
- **Linux:** the Bluetooth adapter must send voice over HCI, which almost all
  USB and laptop adapters do. If calls connect but stay silent, the adapter
  sends voice over a PCM bus instead and the Bumble backend with a USB dongle
  is the way out.
- **Windows:** needs a dedicated USB dongle for Bluetooth (see above). Wi-Fi
  mode needs no dongle.
- **Wi-Fi:** call control only, no audio (Android's rule); Android phones only.
  Over Wi-Fi COB can't tell "dialing" from "talking", because Android doesn't
  report it to apps.
- One phone at a time.

## Troubleshooting

- **`UUID already registered`** (Linux): another program (PipeWire,
  PulseAudio, oFono) holds the hands-free role. See the PipeWire step above.
- **`org.freedesktop.DBus.Error.AccessDenied`** (Linux): your user may not
  talk to BlueZ. Run from a desktop session, or add yourself to the group
  allowed by `/usr/share/dbus-1/system.d/bluetooth.conf`.
- **The phone connects but no audio:** run with `-v` and check for
  "SCO connection" lines. Try `x` to pull the audio over. On Linux, make sure
  PipeWire isn't using the headset roles (see above).
- **`LIBUSB_ERROR_NOT_SUPPORTED`** or "no device found" (Windows): the dongle
  still has the Windows driver. Redo the Zadig step.
- **Choppy audio:** try `--audio-rate 16000`, or pick the devices explicitly
  with `--input-device` and `--output-device`.
- **The app says "Looking for <PC>…"** (Wi-Fi): check that the phone and PC
  are on the same network, COB runs with `--wifi`, and the firewall allows
  port 7480. Some guest networks isolate devices from each other.
- **Wrong address in the QR code:** start COB with `--wifi-host <PC's IP>`.
- **The app stops overnight:** tap *Keep running in the background* in the
  app and allow it, so battery saving doesn't cut the link.

## Development

```bash
pip install -e ".[dev]"
pytest
```

The tests run without Bluetooth hardware. A fake phone exercises the HFP
protocol, two virtual Bumble controllers run the Windows backend end to end,
and a fake companion app exercises the encrypted Wi-Fi link. CI runs them on
Linux and Windows and builds the Android app.

## License

GPL-3.0. See [LICENSE](LICENSE).
