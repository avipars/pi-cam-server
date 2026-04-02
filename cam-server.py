#!/usr/bin/python3

# Code is forked from picamera2 examples and heavily modified with improvements

import io
import logging
import socketserver
from http import server
from threading import Condition, Thread
from picamera2 import Picamera2
from picamera2.encoders import MJPEGEncoder
from picamera2.outputs import FileOutput
from datetime import datetime
import time
import piexif
import os
# Page template with responsive styling
PAGE = """\
<html>
<head>
<title>Pi Camera Feed</title>
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<style>
    body {{ font-family: Arial, sans-serif; text-align: center; margin: 0; padding: 0; }}
    img {{ width: 100%; height: auto; max-width: 1000px; }}
    #controls {{ margin-top: 10px; display: flex; flex-wrap: wrap; justify-content: center; }}
    #controls button {{ 
        margin: 5px;
        padding: 10px 20px;
        font-size: 1rem;
        flex: 1 1 30%;
        max-width: 100px;
        border: none;
        border-radius: 5px;
        background-color: #007BFF;
        color: white;
    }}
    #controls button:hover {{ background-color: #0056b3; }}
    #info {{ margin-top: 15px; font-size: 0.9rem; color: #333; }}
    h1 {{ font-size: 1.5rem; margin: 10px 0; }}
</style>
<script>
    function sendCommand(command) {{
        fetch('/control?command=' + command)
            .then(response => response.text())
            .then(data => console.log(data));
    }}
</script>
</head>
<body>
<h1>Pi Camera Feed</h1>
<img src="stream.mjpg" width="{WIDTH}" height="{HEIGHT}" alt="Camera Feed"/>
<div id="controls">
    <p>(Beta) Bonus Buttons</p>
    <button onclick="fetch('/rotate')">Rotate Camera</button>
    <button onclick="sendCommand('zoom_in')">Zoom In</button>
    <button onclick="sendCommand('zoom_out')">Zoom Out</button>
    <button onclick="sendCommand('resolution_high')">High Resolution</button>
    <button onclick="sendCommand('resolution_low')">Low Resolution</button>
</div>
<div id="info">
    <p>Date: {date} | CPU Usage: {cpu}% | Temperature: {temp} </p>
</div>
</body>
</html>
"""

# Initial rotation state
ROTATION = 270  # Start with 270 degrees
WIDTH = 640
HEIGHT = 480

PORT = 8000 # web port

def update_rotation_header(rotation):
    """Update EXIF rotation header based on the rotation angle."""
    global WIDTH, HEIGHT, rotation_header
    rotation_header = bytes()
    if rotation == 90 or rotation == 270:
        WIDTH, HEIGHT = HEIGHT, WIDTH  # Swap dimensions for 90 or 270
    else:
        WIDTH, HEIGHT = 640, 480  # Original dimensions for 0 rotation

    code = {0: 1, 90: 6, 270: 8}.get(rotation, 1)
    exif_bytes = piexif.dump({'0th': {piexif.ImageIFD.Orientation: code}})
    exif_len = len(exif_bytes) + 2
    rotation_header = bytes.fromhex('ffe1') + exif_len.to_bytes(2, 'big') + exif_bytes

# Functions to get CPU usage and temperature
def get_cpu_usage():
    with open('/proc/stat', 'r') as f:
        line = f.readline()
    fields = [float(column) for column in line.strip().split()[1:]]
    idle_time, total_time = fields[3], sum(fields)
    usage = 100 * (1 - idle_time / total_time)
    return round(usage, 2)

def get_cpu_temp():
    with open('/sys/class/thermal/thermal_zone0/temp', 'r') as f:
        temp_str = f.read().strip()
    temp = float(temp_str) / 1000
    return round(temp, 2)

def get_metadata():
    return picam2.capture_metadata()

def change_rotation():
    """Cycle rotation between 0, 90, and 270, and restart camera with new settings."""
    global ROTATION
    global WIDTH
    global HEIGHT
    picam2.stop_recording()  # Stop camera first

    # Cycle through 0 -> 90 -> 270 -> 0 degrees
    ROTATION = (ROTATION + 90) % 360
    if ROTATION not in [0, 90, 270]:  # Reset to 0 if out of range
        ROTATION = 0

    # Update EXIF header based on the new rotation
    update_rotation_header(ROTATION)

    # Reconfigure and restart camera
    picam2.configure(picam2.create_video_configuration(main={"size": (WIDTH, HEIGHT)}))
    picam2.start_recording(MJPEGEncoder(), FileOutput(output))


class StreamingOutput(io.BufferedIOBase):
    def __init__(self):
        self.frame = None
        self.condition = Condition()

    def write(self, buf):
        with self.condition:
            self.frame = buf[:2] + rotation_header + buf[2:]
            self.condition.notify_all()

class StreamingHandler(server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/':
            self.send_response(301)
            self.send_header('Location', '/index.html')
            self.end_headers()
        elif self.path == '/index.html':
            content = self.server.page_content().encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.send_header('Content-Length', len(content))
            self.end_headers()
            self.wfile.write(content)
        elif self.path == '/stream.mjpg':
            self.send_response(200)
            self.send_header('Age', 0)
            self.send_header('Cache-Control', 'no-cache, private')
            self.send_header('Pragma', 'no-cache')
            self.send_header('Content-Type', 'multipart/x-mixed-replace; boundary=FRAME')
            self.end_headers()
            try:
                while True:
                    with output.condition:
                        output.condition.wait()
                        frame = output.frame
                    self.wfile.write(b'--FRAME\r\n')
                    self.send_header('Content-Type', 'image/jpeg')
                    self.send_header('Content-Length', len(frame))
                    self.end_headers()
                    self.wfile.write(frame)
                    self.wfile.write(b'\r\n')
            except Exception as e:
                logging.warning(
                    'Removed streaming client %s: %s',
                    self.client_address, str(e))
        elif self.path.startswith('/control'):
            command = self.path.split('=')[1]
            self.server.handle_command(command)
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain')
            self.end_headers()
            self.wfile.write(b'Command executed')
        elif self.path == '/rotate':
            # Change rotation and restart camera
            change_rotation()
            self.send_response(200)
            self.end_headers()
        else:
            self.send_error(404)
            self.end_headers()

class StreamingServer(socketserver.ThreadingMixIn, server.HTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, server_address, RequestHandlerClass, page_update_interval=60):
        super().__init__(server_address, RequestHandlerClass)
        self.page_update_interval = page_update_interval
        self.page_content_cache = ""
        self.update_page_content()
        self.start_background_page_updater()

    def page_content(self):
        return self.page_content_cache

    def update_page_content(self):
        global WIDTH
        global HEIGHT
        # Get system stats
        date = datetime.now().strftime('%Y-%m-%d')
        # time = datetime.now().strftime('%H:%M')
        temp = get_cpu_temp()
        cpu = get_cpu_usage()
        # metadata = get_metadata()
        # Update the HTML content with the stats
        self.page_content_cache = PAGE.format(WIDTH=WIDTH, HEIGHT=HEIGHT,date=date, cpu=cpu, temp=temp)

    def start_background_page_updater(self):
        def update_task():
            while True:
                self.update_page_content()
                time.sleep(self.page_update_interval)
        updater_thread = Thread(target=update_task, daemon=True)
        updater_thread.start()

    def handle_command(self, command):
        global picam2
        global WIDTH
        global HEIGHT
        if command == "zoom_in":
            size = picam2.capture_metadata()['ScalerCrop'][2:]
            full_res = picam2.camera_properties['PixelArraySize']
            for _ in range(20):
                # This syncs us to the arrival of a new camera frame:
                picam2.capture_metadata()
                size = [int(s * 0.95) for s in size]
                offset = [(r - s) // 2 for r, s in zip(full_res, size)]
                picam2.set_controls({"ScalerCrop": offset + size})
        elif command == "zoom_out":
            size = picam2.capture_metadata()['ScalerCrop'][2:]
            full_res = picam2.camera_properties['PixelArraySize']
            for _ in range(20):
                # This syncs us to the arrival of a new camera frame:
                picam2.capture_metadata()
                size = [int(min(s * 1.05, r)) for s, r in zip(size, full_res)]
                offset = [(r - s) // 2 for r, s in zip(full_res, size)]
                picam2.set_controls({"ScalerCrop": offset + size})
        elif command == "resolution_high":
            WIDTH = 1280
            HEIGHT = 720
            self.change_resolution((WIDTH, HEIGHT))
        elif command == "resolution_low":
            WWIDTH = 640
            HEIGHT = 480
            self.change_resolution((WIDTH, HEIGHT))
        else: 
            logging.error(f"Commnad doesn't exist: {command}")
        logging.info(f"Executed command: {command}")

    def change_resolution(self, size):
        global picam2, output
        picam2.stop_recording()
        picam2.configure(picam2.create_video_configuration(main={"size": size}))
        picam2.start_recording(MJPEGEncoder(), FileOutput(output))

if __name__ == "__main__":
    # Initialize camera and server
    picam2 = Picamera2()
    update_rotation_header(ROTATION)  # Set initial rotation header

    picam2.configure(picam2.create_video_configuration(main={"size": (WIDTH, HEIGHT)}))
    output = StreamingOutput()
    picam2.start_recording(MJPEGEncoder(), FileOutput(output))

    try:
        address = ('', PORT)
        server = StreamingServer(address, StreamingHandler)
        logger.info(f"Serving at http://0.0.0.0:{PORT}")
        server.serve_forever()
    finally:
        picam2.stop_recording()
