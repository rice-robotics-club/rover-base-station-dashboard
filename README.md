# Dual Camera WebRTC Streaming System

Stream two cameras simultaneously from GStreamer to a web browser using WebRTC.

## Architecture

Each camera has:
- Its own GStreamer pipeline (`v4l2src → x264enc → webrtcbin`)
- Independent WebRTC peer connection
- Separate signaling channel (identified by `camera` field in messages)

## Configuration

Edit camera device paths in `dual_camera_sender.py`:

```python
CAMERAS = {
    "camera1": "/dev/video0",  # First camera
    "camera2": "/dev/video2",  # Second camera
}
```

### Finding Your Camera Devices

List available cameras:
```bash
ls -la /dev/video*
v4l2-ctl --list-devices
```

Test a camera:
```bash
gst-launch-1.0 v4l2src device=/dev/video0 ! autovideosink
```

## Usage

### 1. Start the signaling server
```bash
python3 signaling_server.py
```

### 2. Start the dual camera sender
```bash
python3 dual_camera_sender.py
```

You should see:
```
[camera1] Pipeline created for /dev/video0
[camera2] Pipeline created for /dev/video2
[camera1] Pipeline started
[camera2] Pipeline started
Waiting for browser connections...
```

### 3. Open the viewer in your browser
```
http://localhost:8080/dual_camera_viewer.html
```

Click "Connect Both Cameras" and both video feeds should appear side-by-side.

## Key Features

✅ **Independent pipelines** — each camera runs in its own pipeline
✅ **Simultaneous streaming** — both cameras stream at the same time
✅ **Clean separation** — signaling messages tagged with camera ID
✅ **Graceful reconnection** — disconnect and reconnect without restarting sender
✅ **Separate status indicators** — see connection state per camera

## Signaling Protocol

Messages include a `camera` field to identify which camera:

```json
// Browser → Server
{"type": "ready", "camera": "camera1"}
{"type": "answer", "camera": "camera1", "sdp": "..."}

// Server → Browser
{"type": "offer", "camera": "camera1", "sdp": "..."}
{"type": "candidate", "camera": "camera1", "candidate": "..."}
```

## Troubleshooting

### Camera not detected
```bash
# Check camera permissions
sudo usermod -a -G video $USER
# Log out and back in

# Verify camera is accessible
v4l2-ctl --list-devices
```

### One camera works, other doesn't
- Check device paths in CAMERAS dict
- Verify both cameras support 640×480 @ 30fps:
  ```bash
  v4l2-ctl --device=/dev/video0 --list-formats-ext
  ```

### Pipeline errors
Enable GStreamer debug:
```bash
GST_DEBUG=3 python3 dual_camera_sender.py
```

### Browser shows black screen
- Check browser console for WebRTC errors
- Verify ICE candidates are being exchanged (check logs)
- Try a different browser (Chrome/Firefox recommended)

## Scaling to More Cameras

To add more cameras:

1. Add to `CAMERAS` dict:
```python
CAMERAS = {
    "camera1": "/dev/video0",
    "camera2": "/dev/video2",
    "camera3": "/dev/video4",  # Add this
}
```

2. Add HTML in viewer:
```html
<div class="camera-card">
  <div class="camera-header">
    <div class="camera-title">Camera 3</div>
    ...
  </div>
  <div class="video-container">
    <video id="video-cam3" autoplay playsinline muted></video>
    ...
  </div>
</div>
```

3. Add to JavaScript cameras object and connect it in `connectAll()`

## Performance Notes

- Each camera at 640×480 @ 30fps uses ~1 Mbps
- Two cameras = ~2 Mbps total bandwidth
- H.264 encoding is CPU-intensive — consider hardware encoding for >2 cameras
- For low-power devices (Raspberry Pi), reduce resolution or framerate:
  ```python
  # In pipeline_str, change to:
  video/x-raw,width=320,height=240,framerate=15/1
  ```

## Next Steps

- **Hardware encoding**: Replace `x264enc` with `omxh264enc` (RPi) or `nvh264enc` (Nvidia)
- **Adaptive bitrate**: Monitor network conditions and adjust encoder bitrate
- **Recording**: Add `tee` element to save streams while streaming
- **Audio**: Add audio pipelines from microphones