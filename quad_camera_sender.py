"""
QUAD CAMERA GSTREAMER + WEBRTC SYSTEM
======================================
Streams four cameras simultaneously to the browser.
Each camera has its own pipeline and webrtcbin instance.
"""

import asyncio
import json
import logging
import sys
from typing import Dict, Optional

import gi
gi.require_version("Gst", "1.0")
gi.require_version("GstWebRTC", "1.0")
gi.require_version("GstSdp", "1.0")
from gi.repository import Gst, GstWebRTC, GstSdp, GLib

Gst.init(None)
import aiohttp

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Configuration
SIGNALING_SERVER_URL = "ws://localhost:8080/ws"
STUN_SERVER = "stun://stun.l.google.com:19302"

# Camera configurations - adjust device paths as needed
CAMERAS = {
    "camera1": "/dev/video0",  # First camera
    "camera2": "/dev/video3",  # Second camera
    "camera3": "/dev/video2",  # Third camera
    "camera4": "/dev/video2",  # Fourth camera
}


def build_pipeline(camera_id: str, device: str) -> tuple:
    """Build pipeline for one camera."""
    pipeline_str = f"""
    webrtcbin name=webrtc_{camera_id} bundle-policy=max-bundle stun-server={STUN_SERVER}
    
    v4l2src device={device} name=src_{camera_id} !
    video/x-raw,width=640,height=480,framerate=30/1 !
    videoconvert !
    video/x-raw,format=I420 !
    queue !
    x264enc tune=zerolatency speed-preset=ultrafast bitrate=1000 key-int-max=30 !
    rtph264pay config-interval=1 pt=96 !
    queue !
    webrtc_{camera_id}.
    """
    
    pipeline = Gst.parse_launch(pipeline_str)
    webrtcbin = pipeline.get_by_name(f"webrtc_{camera_id}")
    
    return pipeline, webrtcbin


class CameraStream:
    """Manages one camera's WebRTC stream."""
    
    def __init__(self, camera_id: str, device: str, ws, loop):
        self.camera_id = camera_id
        self.device = device
        self.ws = ws
        self.loop = loop  # asyncio event loop
        
        # State
        self.pipeline = None
        self.webrtcbin = None
        self.session_count = 0
        self.offer_created = asyncio.Event()
        
        # Build and setup pipeline
        self._create_pipeline()
        
        logger.info(f"[{camera_id}] Pipeline created for {device}")
    
    def _create_pipeline(self):
        """Create or recreate the pipeline."""
        # Build pipeline
        self.pipeline, self.webrtcbin = build_pipeline(self.camera_id, self.device)
        
        # Connect signals
        self.webrtcbin.connect("on-negotiation-needed", self.on_negotiation_needed)
        self.webrtcbin.connect("on-ice-candidate", self.on_ice_candidate)
        
        # Bus for errors
        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self.on_bus_message)
    
    def reset(self):
        """Reset the pipeline for a new connection."""
        logger.info(f"[{self.camera_id}] Resetting pipeline for new connection")
        
        # Stop old pipeline
        if self.pipeline:
            self.pipeline.set_state(Gst.State.NULL)
        
        # Recreate pipeline with fresh webrtcbin
        self._create_pipeline()
        
        # Start the new pipeline
        ret = self.pipeline.set_state(Gst.State.PLAYING)
        if ret == Gst.StateChangeReturn.FAILURE:
            logger.error(f"[{self.camera_id}] Failed to restart pipeline")
            return False
        
        logger.info(f"[{self.camera_id}] Pipeline reset and restarted")
        return True
    
    def start(self):
        """Start the pipeline."""
        ret = self.pipeline.set_state(Gst.State.PLAYING)
        if ret == Gst.StateChangeReturn.FAILURE:
            logger.error(f"[{self.camera_id}] Failed to start pipeline")
            return False
        logger.info(f"[{self.camera_id}] Pipeline started")
        return True
    
    def on_negotiation_needed(self, webrtcbin):
        """Create and send SDP offer."""
        logger.info(f"[{self.camera_id}] Creating offer...")
        promise = Gst.Promise.new_with_change_func(self._on_offer_created, webrtcbin, None)
        webrtcbin.emit("create-offer", None, promise)
    
    def _on_offer_created(self, promise, user_data, *args):
        """Handle offer creation. Accepts variable args for GStreamer compatibility."""
        reply = promise.get_reply()
        offer = reply.get_value("offer")
        
        promise = Gst.Promise.new()
        self.webrtcbin.emit("set-local-description", offer, promise)
        promise.interrupt()
        
        # Send to browser
        sdp_text = offer.sdp.as_text()
        msg = {
            "type": "offer",
            "camera": self.camera_id,
            "sdp": sdp_text
        }
        # Schedule on asyncio loop from GLib thread
        self.loop.call_soon_threadsafe(
            asyncio.create_task, self._send_message(msg)
        )
        logger.info(f"[{self.camera_id}] Offer sent ({len(sdp_text)} bytes)")
    
    def on_ice_candidate(self, webrtcbin, mline_index, candidate):
        """Forward ICE candidates to browser."""
        msg = {
            "type": "candidate",
            "camera": self.camera_id,
            "candidate": candidate,
            "sdpMLineIndex": mline_index
        }
        # Schedule on asyncio loop from GLib thread
        self.loop.call_soon_threadsafe(
            asyncio.create_task, self._send_message(msg)
        )
    
    async def _send_message(self, msg):
        """Send message via WebSocket."""
        try:
            await self.ws.send_json(msg)
        except Exception as e:
            logger.error(f"[{self.camera_id}] Send failed: {e}")
    
    async def handle_answer(self, sdp_text: str):
        """Handle SDP answer from browser."""
        logger.info(f"[{self.camera_id}] Received answer")
        ret, sdp_msg = GstSdp.SDPMessage.new_from_text(sdp_text)
        
        if ret != GstSdp.SDPResult.OK:
            logger.error(f"[{self.camera_id}] Failed to parse SDP")
            return
        
        answer = GstWebRTC.WebRTCSessionDescription.new(
            GstWebRTC.WebRTCSDPType.ANSWER, sdp_msg
        )
        
        promise = Gst.Promise.new()
        self.webrtcbin.emit("set-remote-description", answer, promise)
        promise.interrupt()
    
    async def handle_ice_candidate(self, candidate: str, sdp_mline_index: int):
        """Add remote ICE candidate."""
        self.webrtcbin.emit("add-ice-candidate", sdp_mline_index, candidate)
    
    def on_bus_message(self, bus, message):
        """Handle pipeline errors."""
        t = message.type
        if t == Gst.MessageType.ERROR:
            err, debug = message.parse_error()
            logger.error(f"[{self.camera_id}] Pipeline error: {err} | {debug}")
        elif t == Gst.MessageType.WARNING:
            warn, debug = message.parse_warning()
            logger.warning(f"[{self.camera_id}] Pipeline warning: {warn}")
    
    def stop(self):
        """Stop the pipeline."""
        self.pipeline.set_state(Gst.State.NULL)
        logger.info(f"[{self.camera_id}] Pipeline stopped")


class DualCameraStreamer:
    """Manages multiple camera streams."""
    
    def __init__(self):
        self.ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self.cameras: Dict[str, CameraStream] = {}
        self.glib_loop = None
        self.glib_thread = None
    
    def start_glib_loop(self):
        """Start GLib main loop in background thread."""
        import threading
        
        def run_loop():
            self.glib_loop = GLib.MainLoop()
            self.glib_loop.run()
        
        self.glib_thread = threading.Thread(target=run_loop, daemon=True)
        self.glib_thread.start()
        logger.info("GLib main loop started")
    
    async def connect_signaling(self):
        """Connect to signaling server."""
        session = aiohttp.ClientSession()
        self.ws = await session.ws_connect(SIGNALING_SERVER_URL)
        logger.info("Connected to signaling server")
    
    async def initialize_cameras(self):
        """Create pipeline for each camera."""
        loop = asyncio.get_running_loop()
        for camera_id, device in CAMERAS.items():
            self.cameras[camera_id] = CameraStream(camera_id, device, self.ws, loop)
    
    async def start_cameras(self):
        """Start all camera pipelines."""
        for camera_id, camera in self.cameras.items():
            if not camera.start():
                logger.error(f"Failed to start {camera_id}")
    
    async def handle_signaling(self):
        """Process incoming signaling messages."""
        async for msg in self.ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                data = json.loads(msg.data)
                sig_type = data.get("type")
                camera_id = data.get("camera")
                
                logger.info(f"Signal: {sig_type} for {camera_id}")
                
                if sig_type == "ready" and camera_id:
                    # Browser ready for this camera - reset pipeline for fresh connection
                    camera = self.cameras.get(camera_id)
                    if camera:
                        camera.session_count += 1
                        # Reset pipeline to clear stale WebRTC state (for reconnections)
                        if camera.session_count > 1:
                            camera.reset()
                        else:
                            # First connection - just trigger negotiation
                            camera.webrtcbin.emit("on-negotiation-needed")
                
                elif sig_type == "answer" and camera_id:
                    camera = self.cameras.get(camera_id)
                    if camera:
                        await camera.handle_answer(data["sdp"])
                
                elif sig_type == "candidate" and camera_id:
                    camera = self.cameras.get(camera_id)
                    if camera and data.get("candidate"):
                        await camera.handle_ice_candidate(
                            data["candidate"],
                            data.get("sdpMLineIndex", 0)
                        )
                
                elif sig_type == "disconnect" and camera_id:
                    # Browser disconnected - prepare for reconnection
                    camera = self.cameras.get(camera_id)
                    if camera:
                        logger.info(f"[{camera_id}] Browser disconnected, resetting pipeline")
                        camera.reset()
            
            elif msg.type == aiohttp.WSMsgType.ERROR:
                logger.error(f"WebSocket error: {self.ws.exception()}")
                break
    
    async def run(self):
        """Main entry point."""
        self.start_glib_loop()
        await self.connect_signaling()
        await self.initialize_cameras()
        await self.start_cameras()
        
        logger.info("Waiting for browser connections...")
        
        try:
            await self.handle_signaling()
        except KeyboardInterrupt:
            logger.info("Shutting down...")
        finally:
            for camera in self.cameras.values():
                camera.stop()
            
            if self.glib_loop:
                self.glib_loop.quit()


if __name__ == "__main__":
    streamer = DualCameraStreamer()
    asyncio.run(streamer.run())