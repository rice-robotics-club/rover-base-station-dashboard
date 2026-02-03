"""
SIGNALING SERVER — signaling_server.py
======================================
What this file does:
    WebRTC, by itself, cannot set up a connection. Two peers need to exchange
    "signaling" information (SDP offers/answers and ICE candidates) before
    they can open a direct peer-to-peer media channel. This server is that
    exchange point.

Architecture role:
    GStreamer (producer)  ──WebSocket──►  THIS SERVER  ──WebSocket──►  Browser (consumer)
                          SDP offer                      SDP offer
                          ICE candidates                 ICE candidates
                          ◄── SDP answer                 ◄── SDP answer
                          ◄── ICE candidates             ◄── ICE candidates

Why aiohttp?
    It supports both HTTP (to serve the HTML page) and WebSockets in the
    same async event loop — no need for two separate servers.

Message protocol (simple JSON over WebSocket):
    { "type": "offer",     "sdp": "<SDP string>" }
    { "type": "answer",    "sdp": "<SDP string>" }
    { "type": "candidate","candidate": "<ICE candidate string>","sdpMLineIndex": <int> }
"""

import json
import logging
from aiohttp import web
import aiohttp
import os

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Global state: track connected WebSocket clients so we can broadcast signals
# ---------------------------------------------------------------------------
# We keep references to every open WebSocket.  When GStreamer sends an offer,
# we forward it to all browser clients (and vice-versa).  For a single-camera
# prototype this is fine; for multi-camera you'd key by camera ID.
# ---------------------------------------------------------------------------
connected_clients: set[web.WebSocketResponse] = set()


# ---------------------------------------------------------------------------
# HTTP route: serve the HTML viewer page
# ---------------------------------------------------------------------------
async def serve_index(request: web.Request) -> web.FileResponse:
    """Serve index.html from the same directory this script lives in."""
    here = os.path.dirname(os.path.abspath(__file__))
    return web.FileResponse(os.path.join(here, "index.html"))


# ---------------------------------------------------------------------------
# WebSocket route: signaling channel
# ---------------------------------------------------------------------------
async def websocket_handler(request: web.Request) -> web.WebSocketResponse:
    """
    Each browser tab and the GStreamer process each open ONE WebSocket here.
    Any message received on one socket is re-broadcast to every OTHER socket.
    """
    ws = web.WebSocketResponse()
    await ws.prepare(request)

    connected_clients.add(ws)
    logger.info(f"WebSocket connected  — total clients: {len(connected_clients)}")

    async for msg in ws:
        if msg.type == aiohttp.WSMsgType.TEXT:
            payload = msg.data
            logger.info(f"Received signal: {payload[:120]}...")

            # Broadcast to every OTHER connected WebSocket
            for client in connected_clients:
                if client is not ws and not client.closed:
                    await client.send_str(payload)

        elif msg.type == aiohttp.WSMsgType.ERROR:
            logger.warning(f"WebSocket error: {ws.exception()}")

    # Cleanup on disconnect
    connected_clients.discard(ws)
    logger.info(f"WebSocket disconnected — total clients: {len(connected_clients)}")
    return ws


# ---------------------------------------------------------------------------
# Application factory & startup
# ---------------------------------------------------------------------------
def create_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/", serve_index)            # HTML page
    app.router.add_get("/ws", websocket_handler)    # WebSocket endpoint
    return app


if __name__ == "__main__":
    app = create_app()
    # 0.0.0.0 so it's reachable on the LAN (needed if browser is on another machine)
    web.run_app(app, host="0.0.0.0", port=8080)
