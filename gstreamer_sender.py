"""
GSTREAMER + WEBRTC PIPELINE — gstreamer_sender.py
==================================================
What this file does:
    1. Opens the system webcam via GStreamer.
    2. Encodes every frame to H.264 (the codec your rover cameras will also use).
    3. Feeds the encoded stream into a `webrtcbin` element.
    4. Handles the WebRTC signaling dance (offer/answer + ICE candidates)
       by talking to the signaling server over a WebSocket.

Why GStreamer + webrtcbin instead of just using the browser?
    On the rover, the camera feeds will come from Linux-based embedded
    hardware.  GStreamer is the standard tool for that pipeline.  By
    building and testing the GStreamer → webrtcbin path NOW (with a webcam),
    the only thing you change later is the SOURCE element at the top of the
    pipeline (e.g. `v4l2src` device path or an IP camera source).

Pipeline diagram:
    ┌────────────┐   ┌─────────────┐   ┌──────────┐   ┌────────────┐
    │ v4l2src    │──►│ videoconvert│──►│ x264enc  │──►│ webrtcbin  │──► (UDP to browser)
    │ (webcam)   │   │             │   │ (H.264)  │   │            │
    └────────────┘   └─────────────┘   └──────────┘   └────────────┘

Key GStreamer concepts used here:
    - Bus messages: GStreamer's internal event bus tells us when webrtcbin
      creates an SDP offer or discovers an ICE candidate.
    - Pad linking: webrtcbin dynamically creates pads; we listen for the
      "pad-added" signal to connect our video stream into it.
    - Transceiver: We add a transceiver BEFORE starting the pipeline so
      webrtcbin knows to expect a video track.

Requirements (install via your package manager):
    Python:     gst-python3  (PyGObject bindings)
    GStreamer:  gstreamer1.0-base
                gstreamer1.0-plugins-base        (videoconvert, etc.)
                gstreamer1.0-plugins-good        (v4l2src, rtpvp8pay, etc.)
                gstreamer1.0-plugins-bad         (webrtcbin, dtls, etc.)
                gstreamer1.0-plugins-ugly        (x264enc)
    Python pkg: aiohttp  (pip install aiohttp)  — used for both the signaling
                         server AND the WebSocket client in this script

    On Ubuntu/Debian:
        sudo apt install python3-gst-1.0 \
             gstreamer1.0-base \
             gstreamer1.0-plugins-base \
             gstreamer1.0-plugins-good \
             gstreamer1.0-plugins-bad \
             gstreamer1.0-plugins-ugly \
             gstreamer1.0-libav
        pip install aiohttp
"""

import asyncio
import json
import logging
import sys

import gi
gi.require_version("Gst", "1.0")
gi.require_version("GstWebRTC", "1.0")
gi.require_version("GstSdp", "1.0")
from gi.repository import Gst, GstWebRTC, GstSdp, GLib

# Initialise GStreamer — MUST happen before any Gst.* call (e.g. parse_launch).
# Passing None is equivalent to passing sys.argv; GStreamer uses it to pull
# out any gst-specific flags (like --gst-debug) but doesn't require them.
Gst.init(None)

import aiohttp

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
SIGNALING_SERVER_URL = "ws://localhost:8080/ws"   # where the signaling server lives
WEBCAM_DEVICE       = "/dev/video0"               # change if you have multiple webcams

# ---------------------------------------------------------------------------
# GStreamer pipeline construction
# ---------------------------------------------------------------------------
def build_pipeline() -> Gst.Pipeline:
    """
    Assemble the full pipeline as a string and let GStreamer parse it.
    This is equivalent to running the gst-launch-1.0 command on the CLI,
    but gives us a handle to the webrtcbin element so we can wire up
    signaling callbacks.

    Pipeline breakdown:
        v4l2src device=/dev/video0   — captures raw frames from the webcam
            → videoscale               — resize to 640×480 (keep bandwidth low for testing)
            → videoconvert             — pixel-format conversion (YUV variants)
            → x264enc                  — software H.264 encoder
                  tune=zerolatency     — minimizes encoder buffering → lower latency
                  speed-preset=ultrafast
            → rtph264pay               — packetises the H.264 bitstream into RTP packets
                  (WebRTC transmits media as RTP over UDP)
            → webrtcbin                — the GStreamer WebRTC engine
                  stun-server          — STUN server helps peers behind NATs discover
                                         their public IP (needed if not on same LAN)
    """
    # We CANNOT put webrtcbin in the same parse string as the encoder chain.
    # webrtcbin's sink pad is created dynamically only after a transceiver is
    # added — so at parse time there is no pad to link to and it fails.
    # Solution: parse the two halves separately, add them both to one Pipeline,
    # and link them manually later (after the transceiver exists).

    pipeline = Gst.Pipeline.new("rover-pipeline")

    # --- encoder chain: webcam → scale → convert → H.264 → RTP packetiser ---
    encoder_chain_str = (
        f"v4l2src device={WEBCAM_DEVICE} ! "
        "videoconvert ! "                                  # convert from whatever the webcam outputs
        "videoscale ! "                                    # then scale down
        "video/x-raw,width=640,height=480 ! "              # target size (no framerate lock)
        "videoconvert ! "                                  # ensure x264enc gets what it wants
        "x264enc tune=zerolatency speed-preset=ultrafast ! "
        "rtph264pay mtu=1200 name=rtph264pay"
    )
    encoder_bin = Gst.parse_bin_from_description(encoder_chain_str, True)

    # --- webrtcbin (standalone, no links yet) --------------------------------
    webrtcbin = Gst.ElementFactory.make("webrtcbin", "webrtcbin")
    webrtcbin.set_property("stun-server", "stun://stun.l.google.com:19302")
    webrtcbin.set_property("bundle-policy", 3)  # 3 = max-bundle

    # Add both to the pipeline (but do NOT link — that happens after
    # the transceiver is added in setup_pipeline).
    pipeline.add(encoder_bin)
    pipeline.add(webrtcbin)

    return pipeline


# ---------------------------------------------------------------------------
# The signaling & ICE negotiation logic
# ---------------------------------------------------------------------------
class WebRTCSender:
    """
    Owns the GStreamer pipeline and the WebSocket connection to the signaling
    server.  Coordinates the offer/answer/ICE dance between them.

    Lifecycle:
        1. connect()         — open WebSocket to signaling server
        2. start()           — launch GStreamer pipeline
        3.   (auto) webrtcbin emits "create-offer" → we send offer over WS
        4.   (auto) browser sends back an answer    → we feed it to webrtcbin
        5.   ICE candidates fly both ways via WS
        6.   Media flows over the peer connection
    """

    def __init__(self):
        self.pipeline: Gst.Pipeline | None = None
        self.webrtcbin: Gst.Element | None = None
        self.session = None                       # aiohttp.ClientSession
        self.ws = None                            # aiohttp WebSocket response
        self.loop: asyncio.AbstractEventLoop | None = None
        self.browser_ready = asyncio.Event()      # set when browser sends "ready"

    # ----- WebSocket connection ---------------------------------------------
    async def connect(self):
        """Open a persistent WebSocket to the signaling server via aiohttp."""
        self.session = aiohttp.ClientSession()
        self.ws = await self.session.ws_connect(SIGNALING_SERVER_URL)
        logger.info("Connected to signaling server.")

    # ----- Pipeline setup ---------------------------------------------------
    def setup_pipeline(self):
        """
        Build the GStreamer pipeline and attach all the signal handlers that
        GStreamer will fire during the WebRTC negotiation.
        """
        self.pipeline = build_pipeline()
        self.webrtcbin = self.pipeline.get_by_name("webrtcbin")

        # --- Find the encoder bin (we need it in the pad-added callback) -----
        self.encoder_bin = None
        for child in self.pipeline.iterate_elements():
            if child.get_name() != "webrtcbin":
                self.encoder_bin = child
                break
        if self.encoder_bin is None:
            raise RuntimeError("Could not find encoder bin in pipeline")

        # --- Signal: "pad-added" on webrtcbin ---------------------------------
        # webrtcbin does NOT create its sink pad until the pipeline is running
        # and it is ready to receive media.  "pad-added" fires at that moment.
        # This is where we do the actual link from the encoder chain into
        # webrtcbin — not before.
        self.webrtcbin.connect("pad-added", self.on_webrtcbin_pad_added)

        # --- Signal: "on-negotiation-needed" ----------------------------------
        # Fired when webrtcbin realises it needs to (re-)negotiate with the
        # remote peer (e.g. after adding a transceiver).  We respond by asking
        # webrtcbin to create an SDP offer.
        self.webrtcbin.connect("on-negotiation-needed", self.on_negotiation_needed)

        # --- Signal: "on-ice-candidate" ---------------------------------------
        # Fired each time the local ICE agent discovers a new way to reach this
        # machine (e.g. a STUN-mapped address).  We forward each candidate to
        # the browser via the signaling server.
        self.webrtcbin.connect("on-ice-candidate", self.on_ice_candidate)

        logger.info("Pipeline and signals configured.")

    # ----- GStreamer signal handlers (called by GLib main loop) -------------
    def _add_transceiver(self):
        """
        Called on the GLib main loop thread via idle_add.
        Emitting add-transceiver here ensures on-negotiation-needed is
        delivered on the same thread that runs the loop — required on 1.24.
        After adding, we also try to link the encoder bin immediately —
        on 1.24 the sink pad exists right away but pad-added may not fire.
        Return False so idle_add doesn't keep re-calling us.
        """
        logger.info("Adding transceiver (on GLib thread).")
        result = self.webrtcbin.emit(
            "add-transceiver",
            GstWebRTC.WebRTCRTPTransceiverDirection.SENDONLY,
            None,
        )
        logger.info(f"add-transceiver returned: {result} (type: {type(result)})")

        # Also ask webrtcbin how many transceivers it thinks it has
        transceivers = self.webrtcbin.emit("get-transceivers")
        logger.info(f"get-transceivers returned: {transceivers} (type: {type(transceivers)})")

        # The sink pad may not exist yet — schedule a poller that retries
        # every 50 ms until the pad appears and is linked, then creates the
        # offer.  Return False here so idle_add doesn't repeat _add_transceiver.
        GLib.timeout_add(50, self._poll_link_and_offer)
        return False  # don't repeat

    def _poll_link_and_offer(self):
        """
        Called every 50 ms by GLib.timeout_add.
        Tries to link the encoder pad.  Once linked, creates the offer and
        stops polling (returns False).
        """
        if self._try_link_encoder():
            # Linked! Now create the offer — the transceiver AND pad are ready.
            logger.info("Pad linked — creating offer.")
            promise = Gst.Promise.new()
            self.webrtcbin.emit("create-offer", None, promise)

            import threading
            threading.Thread(
                target=self._wait_and_handle_offer,
                args=(promise,),
                daemon=True,
            ).start()
            return False  # stop polling

        return True  # not linked yet, keep polling

    def _try_link_encoder(self):
        """
        Attempt to link the encoder bin's src pad into webrtcbin's sink pad.
        Returns True if linked successfully, False otherwise.
        """
        src_pad = self.encoder_bin.get_static_pad("src")
        if src_pad is None:
            logger.error("Encoder bin has no 'src' ghost pad!")
            return False

        # Check if webrtcbin already has any sink pads (property, not iterator)
        existing_sinks = self.webrtcbin.sinkpads
        logger.info(f"  webrtcbin sinkpads: {[(p.get_name(), p.is_linked()) for p in existing_sinks]}")

        sink_pad = None
        for p in existing_sinks:
            if not p.is_linked():
                sink_pad = p
                break

        # If no unlinked sink pad exists, request one by name
        if sink_pad is None:
            logger.info("  Requesting sink_0 via request_pad_simple...")
            sink_pad = self.webrtcbin.request_pad_simple("sink_0")
            if sink_pad is not None:
                logger.info(f"  Got requested pad: {sink_pad.get_name()}")
            else:
                logger.info("  request_pad_simple('sink_0') returned None.")
                return False

        if sink_pad.is_linked():
            logger.info("  Sink pad already linked.")
            return True

        logger.info(f"Linking encoder → {sink_pad.get_name()}...")
        link_result = src_pad.link(sink_pad)
        logger.info(f"Pad link result: {link_result}")
        return link_result == Gst.PadLinkReturn.OK

    def on_bus_message(self, bus, message):
        """Log errors and warnings from anywhere inside the pipeline."""
        t = message.type
        if t == Gst.MessageType.ERROR:
            err, debug = message.parse_error()
            logger.error(f"Pipeline ERROR: {err}  debug: {debug}")
        elif t == Gst.MessageType.WARNING:
            warn, debug = message.parse_warning()
            logger.warning(f"Pipeline WARNING: {warn}  debug: {debug}")
        elif t == Gst.MessageType.STATE_CHANGED:
            old, new, pending = message.parse_state_changed()
            logger.info(f"State change: {old} → {new} (pending: {pending})")

    def on_webrtcbin_pad_added(self, element, pad):
        """
        Fired if webrtcbin creates a pad asynchronously.  Just delegate to
        the same linker — it's idempotent (checks is_linked before linking).
        """
        logger.info(f"pad-added signal fired: {pad.get_name()}")
        self._try_link_encoder()

    def on_negotiation_needed(self, element):
        """
        webrtcbin fires this when it thinks negotiation is needed.
        On 1.24 this fires early (before the transceiver is added) and the
        resulting offer is empty.  We ignore it here and create the offer
        explicitly from _add_transceiver after the transceiver and pad link
        are in place.
        """
        logger.info("Negotiation needed signal received (will create offer after transceiver is set up).")

    def _wait_and_handle_offer(self, promise):
        """
        Runs in its own thread.  Blocks on promise.wait() until webrtcbin
        has finished generating the SDP offer, then sets local description
        and sends it to the browser.
        """
        promise.wait()                              # blocks this thread only
        offer = promise.get_reply()                 # retrieve the GstStructure
        if offer is None:
            logger.error("Failed to create offer (reply is None).")
            return

        offer_sdp = offer.get_value("offer")
        logger.info("Offer created — setting local description and sending.")

        # (a) Set as OUR local description  (safe to call from any thread)
        self.webrtcbin.emit("set-local-description", offer_sdp, None)

        # (b) Send over WebSocket  (must schedule on the asyncio loop)
        sdp_string = offer_sdp.sdp.as_text()
        logger.info(f"SDP string length: {len(sdp_string)}")
        self.loop.call_soon_threadsafe(
            asyncio.ensure_future,
            self.send_signal({"type": "offer", "sdp": sdp_string}),
        )

    def on_ice_candidate(self, element, mline_index, candidate):
        """
        Forward a locally-discovered ICE candidate to the browser.
        The browser will do the same in reverse.
        """
        logger.info(f"ICE candidate (local): {candidate}")
        self.loop.call_soon_threadsafe(
            asyncio.ensure_future,
            self.send_signal(
                {
                    "type": "candidate",
                    "candidate": candidate,
                    "sdpMLineIndex": mline_index,
                }
            ),
        )
        self.log_ice_state()

    def log_ice_state(self):
        """Read and log the current ice-connection-state property."""
        state = self.webrtcbin.get_property("ice-connection-state")
        logger.info(f"ICE connection state → {state}")

    # ----- Incoming signals from browser -------------------------------------
    async def handle_remote_signal(self, message: str):
        """
        Dispatch an incoming signaling message from the browser.
        """
        signal = json.loads(message)
        sig_type = signal.get("type")

        if sig_type == "ready":
            # Browser just connected and is listening — safe to start now.
            logger.info("Browser is ready.")
            self.loop.call_soon_threadsafe(self.browser_ready.set)

        elif sig_type == "answer":
            # The browser accepted our offer and sent its SDP answer.
            # We set it as the REMOTE description on webrtcbin.
            logger.info("Received SDP answer from browser.")
            raw_sdp = signal["sdp"]
            logger.info(f"Raw SDP answer ({len(raw_sdp)} bytes):\n{raw_sdp}")
            result = GstSdp.SDPMessage.new_from_text(raw_sdp)
            logger.info(f"new_from_text returned: {result} (type: {type(result)})")
            # new_from_text returns (GstSdpParseReturn, SDPMessage).
            # GstSdpParseReturn: OK=0, means success.
            parse_ret, sdp = result
            logger.info(f"  parse_ret={parse_ret} sdp={sdp}")
            if parse_ret != 0:
                logger.error(f"Failed to parse SDP answer: parse_ret={parse_ret}")
                return
            answer = GstWebRTC.WebRTCSessionDescription.new(
                GstWebRTC.WebRTCSDPType.ANSWER, sdp
            )
            self.webrtcbin.emit("set-remote-description", answer, None)

        elif sig_type == "candidate":
            # The browser discovered one of ITS ICE candidates; add it to
            # our local ICE agent so it knows how to reach the browser.
            logger.info(f"ICE candidate (remote): {signal['candidate']}")
            self.webrtcbin.emit(
                "add-ice-candidate",
                signal["sdpMLineIndex"],
                signal["candidate"],
            )

        else:
            logger.warning(f"Unknown signal type: {sig_type}")

    # ----- WebSocket I/O helpers --------------------------------------------
    async def send_signal(self, payload: dict):
        """Send a JSON message over the WebSocket."""
        await self.ws.send_str(json.dumps(payload))

    async def listen_for_signals(self):
        """
        Continuously read messages from the signaling server and dispatch them.
        aiohttp uses an explicit receive loop instead of async-for.
        """
        async for msg in self.ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                logger.info(f"Signal received: {msg.data[:120]}...")
                await self.handle_remote_signal(msg.data)
            elif msg.type == aiohttp.WSMsgType.ERROR:
                logger.error(f"WebSocket error: {self.ws.exception()}")
                break
            elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSING, aiohttp.WSMsgType.CLOSED):
                logger.warning("WebSocket closed by server.")
                break

    # ----- Main entry point -------------------------------------------------
    async def run(self):
        """
        Orchestrate everything:
            1. Connect to signaling server.
            2. Set up GStreamer pipeline (but don't start yet).
            3. Start the GLib main loop in a background thread (GStreamer needs it).
            4. Start the pipeline.
            5. Listen for signals forever.
        """
        self.loop = asyncio.get_event_loop()

        await self.connect()
        self.setup_pipeline()

        # --- Bus watcher: lets us see errors/warnings from inside the pipeline
        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self.on_bus_message)

        # GStreamer runs its own GLib main loop.  We spin it up in a separate
        # thread so it doesn't block our asyncio loop.
        self.glib_loop = GLib.MainLoop()
        import threading
        glib_thread = threading.Thread(target=self.glib_loop.run, daemon=True)
        glib_thread.start()
        logger.info("GLib main loop started in background thread.")

        # Start listening for signals in the background so we can receive
        # the browser's "ready" message.
        listen_task = asyncio.ensure_future(self.listen_for_signals())

        # Wait for the browser to connect and send "ready" before starting
        # the pipeline.  This prevents the SDP offer from firing before
        # anyone is listening for it.
        logger.info("Waiting for browser to connect…")
        await self.browser_ready.wait()
        logger.info("Browser ready — starting pipeline.")

        # Start the pipeline first — webrtcbin and the GLib loop need to be
        # live before we add the transceiver.
        self.pipeline.set_state(Gst.State.PLAYING)
        logger.info("Pipeline is PLAYING — webcam should be active.")

        # Schedule add-transceiver on the GLib main loop thread via idle_add.
        # Emitting it from the asyncio thread can cause on-negotiation-needed
        # to be silently swallowed on GStreamer 1.24.
        GLib.idle_add(self._add_transceiver)
        logger.info("Transceiver scheduled on GLib loop — waiting for negotiation.")

        # Keep alive
        await listen_task


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    sender = WebRTCSender()
    try:
        asyncio.run(sender.run())
    except KeyboardInterrupt:
        logger.info("Shutting down.")
        if sender.pipeline:
            sender.pipeline.set_state(Gst.State.NULL)