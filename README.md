# Rover Live-Stream — Webcam → GStreamer → WebRTC → Browser

## Architecture at a glance

```
┌──────────────┐     ┌──────────────────┐     ┌─────────────────────┐
│  Webcam      │     │  GStreamer       │     │  Signaling Server   │
│  /dev/video0 │────►│  v4l2src         │     │  (aiohttp)          │
└──────────────┘     │  → videoconvert  │     │                     │
                     │  → x264enc (H264)│     │  Browser ◄──────────┤
                     │  → rtph264pay    │     │    index.html       │
                     │  → webrtcbin ────┼────►│    RTCPeerConnection│
                     └──────────────────┘     └─────────────────────┘
                            │  SDP offer/answer + ICE candidates
                            │  (JSON over WebSocket)
                            ▼
                     ┌──────────────────┐
                     │  Video flows via │
                     │  RTP over UDP    │
                     │  (peer-to-peer)  │
                     └──────────────────┘
```

### Three components, three files

| File                   | Role |
|------------------------|------|
| `signaling_server.py`  | Runs an HTTP + WebSocket server. Serves `index.html` on `/` and relays JSON signaling messages on `/ws`. It never touches media — it only lets GStreamer and the browser *find* each other. |
| `gstreamer_sender.py`  | Captures the webcam, encodes H.264, and pushes the stream into a `webrtcbin`. Connects to the signaling server to exchange SDP and ICE candidates with the browser. |
| `index.html`           | The viewer. Opens a WebSocket to the signaling server, completes the WebRTC handshake, and displays the incoming video. |

---

## Installation

### 1 — Python dependencies

```bash
pip install aiohttp websockets
```

### 2 — GStreamer (Ubuntu / Debian)

```bash
sudo apt update
sudo apt install -i \
  python3-gst-1.0 \
  gstreamer1.0-base \
  gstreamer1.0-plugins-base \
  gstreamer1.0-plugins-good \
  gstreamer1.0-plugins-bad \
  gstreamer1.0-plugins-ugly \
  gstreamer1.0-libav
```

> **macOS / Windows:** GStreamer has official installers at <https://gstreamer.freedesktop.org/download/>.
> The plugin sets you need are *base*, *good*, *bad*, and *ugly*.  The Python bindings (`PyGObject`) are separate on macOS — install via Homebrew: `brew install pygobject3 gstreamer`.

### 3 — Find your webcam device

```bash
# Linux
ls /dev/video*           # usually /dev/video0

# If you're unsure which device is which:
gst-launch-1.0 v4l2src device=/dev/video0 ! autovideosink
```

Edit `WEBCAM_DEVICE` in `gstreamer_sender.py` if it's not `/dev/video0`.

---

## Running

Open **three** terminals.  Order matters slightly — the signaling server must be up first.

### Terminal 1 — Signaling server

```bash
python signaling_server.py
```

You should see:  `======== Running on http://0.0.0.0:8080/ ========`

### Terminal 2 — GStreamer sender

```bash
python gstreamer_sender.py
```

It will connect to the signaling server and wait.  You won't see video yet — it needs a peer to talk to.

### Terminal 3 — Browser

Open `http://localhost:8080` in Chrome, Firefox, or Edge.  Click **Connect**.

The browser and GStreamer will exchange SDP/ICE through the signaling server, and within a second or two the webcam feed should appear.

---

## How the WebRTC handshake works (step by step)

1. **Browser connects** to `ws://localhost:8080/ws`.
2. **GStreamer connects** to the same WebSocket endpoint.
3. GStreamer's `webrtcbin` fires `on-negotiation-needed` → it creates an **SDP offer** (a text description of the media it wants to send and how).
4. The offer is sent as JSON over the WebSocket → the signaling server broadcasts it → the browser receives it.
5. The browser sets the offer as the **remote description** on its `RTCPeerConnection`, then creates an **SDP answer** and sends it back the same way.
6. GStreamer sets the answer as its **remote description**.
7. Meanwhile, both sides are discovering **ICE candidates** (network addresses they can be reached on).  Each candidate is forwarded through the WebSocket.
8. The ICE agents on both sides try every combination of local + remote candidates until one pair works → a direct UDP path is established.
9. RTP video packets (H.264) flow over that UDP path into the browser, which decodes and renders them.

---

## Adapting for the rover

When you have the rover's cameras, you only need to change the **source** element at the top of the GStreamer pipeline in `gstreamer_sender.py`:

| Source type | GStreamer element |
|---|---|
| USB webcam | `v4l2src device=/dev/video0` (what we use now) |
| USB webcam (macOS) | `avfvideosrc` |
| IP / RTSP camera | `rtspsrc location=rtsp://...` |
| GigE Vision | `genicam` or `gige` plugin |
| CSI (Raspberry Pi) | `libcamadaptersrc` |

Everything downstream (x264enc → rtph264pay → webrtcbin → signaling → browser) stays exactly the same.

For **multiple cameras**, run one `gstreamer_sender.py` instance per camera.  The signaling server already broadcasts to all connected WebSocket clients, so each browser tab that connects will receive all streams.  A more production-ready approach would add a camera-ID field to the signaling messages so each viewer can subscribe to a specific camera.

---

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| "WebSocket error" in the browser | Signaling server isn't running, or you're hitting the wrong port. |
| Log says "Negotiation needed" but no offer arrives in the browser | GStreamer connected to the WS *after* the browser — just refresh the browser page. |
| Black video / no `ontrack` | ICE failed.  Check that both sides can reach each other (same LAN, or STUN is working).  Try adding a TURN server to `ICE_CONFIG` in `index.html`. |
| `x264enc` not found | Install `gstreamer1.0-plugins-ugly`. |
| `webrtcbin` not found | Install `gstreamer1.0-plugins-bad`. |
| Permission denied on `/dev/video0` | `sudo usermod -aG video $USER` then log out and back in. |
