#!/usr/bin/env python3
"""
Photobooth — Pi 5 + touchscreen. Camera and printer are independent toggles.

    CAMERA_ENABLED  = False -> uses images from ./samples/ (or a test pattern)
    PRINTER_ENABLED = False -> renders the receipt to PNG, shows it on screen

Right now: both False. Flip PRINTER_ENABLED when the printer arrives, then
CAMERA_ENABLED when the Pi 5 camera cable arrives. Nothing else changes.

Setup (Pi):
    sudo apt install -y chromium-browser python3-picamera2
    python3 -m venv ~/booth/venv --system-site-packages
    source ~/booth/venv/bin/activate
    pip install flask pillow
    # when the printer arrives:  pip install python-escpos
    # (opencv-python is only needed for laptop dev with a webcam — not on the Pi)

Run:
    cd ~/booth && source venv/bin/activate && python booth.py
    chromium-browser --kiosk --noerrdialogs --disable-infobars http://localhost:8000
"""

import io, os, glob, random, threading, time, uuid, datetime, traceback
from flask import Flask, request, jsonify, send_from_directory, Response
from PIL import Image, ImageOps, ImageEnhance, ImageFilter, ImageDraw, ImageFont
# ============================ CONFIG ============================
CAMERA_ENABLED  = True         # picamera2 on the Pi, else any USB/built-in webcam
PRINTER_ENABLED = False        # True once the thermal printer arrives
WEBCAM_INDEX    = 0            # which webcam to use for the fallback backend

VENDOR_ID   = 0x0483           # from `lsusb` — only used when printing
PRODUCT_ID  = 0x5743

WIDTH       = 576              # printable dots, 80mm head @ 203dpi
DITHER      = "floyd"          # "floyd" (fast) | "atkinson" (better, ~3x slower)

BOOTH_NAME  = "HATCHERY BOOTH"
TAGLINE     = "smile - snap - hatch"
CAPTION     = "The Hatchery"           # written on the polaroid top
HANDLE      = "@bc_hatchery"           # next to the logo in the chin
CREDIT      = "built by @s.ofile !"    # small print under the handle

CAPTURE_SIZE = (2028, 1520)
PREVIEW_SIZE = (1012, 760)

# image tuning — this is where booth quality lives
CONTRAST, GAMMA, UNSHARP = 1.15, 0.85, (2, 150, 3)

# layout
MARGIN, GAP = 20, 30
FRAME_W     = WIDTH - 2 * MARGIN
STRIP_SHOTS = 3
STRIP_FRAME_H, SINGLE_FRAME_H = 402, 670
# ================================================================

app = Flask(__name__, static_folder="static", static_url_path="")
RECEIPT_DIR = os.path.join("static", "receipts")
SAMPLE_DIR  = "samples"
os.makedirs(RECEIPT_DIR, exist_ok=True)
os.makedirs(SAMPLE_DIR, exist_ok=True)

session_shots = []
cam = None
stream = None


# ------------------------------------------------------------ camera
class StreamOutput(io.BufferedIOBase):
    def __init__(self):
        self.frame = None
        self.cond = threading.Condition()

    def write(self, buf):
        with self.cond:
            self.frame = buf
            self.cond.notify_all()


CAM_KIND = None                # "picamera" | "webcam" | None
_webcam_last = {"frame": None}

if CAMERA_ENABLED:
    try:
        from picamera2 import Picamera2
        from picamera2.encoders import JpegEncoder
        from picamera2.outputs import FileOutput

        stream = StreamOutput()
        cam = Picamera2()
        cam.configure(cam.create_video_configuration(
            main={"size": CAPTURE_SIZE},
            lores={"size": PREVIEW_SIZE, "format": "YUV420"},
        ))
        cam.start_recording(JpegEncoder(q=80), FileOutput(stream), name="lores")
        time.sleep(1.5)
        CAM_KIND = "picamera"
        # Fixed focus beats autofocus in a booth — AF hunts and misses the moment.
        # from libcamera import controls
        # cam.set_controls({"AfMode": controls.AfModeEnum.Manual,
        #                   "LensPosition": 1/1.2})     # 1.2 metres
    except ImportError:
        # no picamera2 -> USB / built-in webcam via OpenCV (dev on a laptop)
        try:
            import cv2
            _cap = cv2.VideoCapture(WEBCAM_INDEX)
            _cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1920)
            _cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 1080)
            if not _cap.isOpened():
                raise RuntimeError("webcam did not open")
            stream = StreamOutput()

            def _pump():
                while True:
                    ok, f = _cap.read()
                    if not ok:
                        time.sleep(.05)
                        continue
                    _webcam_last["frame"] = f
                    ok2, buf = cv2.imencode(
                        ".jpg", f, [cv2.IMWRITE_JPEG_QUALITY, 80])
                    if ok2:
                        stream.write(buf.tobytes())
                    time.sleep(1 / 30)

            threading.Thread(target=_pump, daemon=True).start()
            time.sleep(1.0)
            CAM_KIND = "webcam"
        except Exception as e:
            print("webcam unavailable:", e, "-> simulated camera")
            CAMERA_ENABLED = False


def test_pattern(n):
    """Stand-in 'photo' when there's no camera and no sample images."""
    img = Image.new("L", CAPTURE_SIZE, 235)
    d = ImageDraw.Draw(img)
    for i in range(0, CAPTURE_SIZE[0], 90):
        d.rectangle([i, 0, i + 90, CAPTURE_SIZE[1]],
                    fill=int(255 * (i / CAPTURE_SIZE[0])))
    d.ellipse([700, 380, 1330, 1140], fill=90)
    try:
        f = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 200)
    except OSError:
        f = ImageFont.load_default()
    d.text((940, 660), str(n), font=f, fill=245)
    return img.convert("RGB")


def grab_frame():
    """One 'capture' — real camera, a sample image, or a test pattern."""
    if CAM_KIND == "picamera":
        return cam.capture_image("main")
    if CAM_KIND == "webcam":
        import cv2
        f = _webcam_last["frame"]
        if f is None:
            raise RuntimeError("webcam not ready")
        return Image.fromarray(cv2.cvtColor(f, cv2.COLOR_BGR2RGB))

    samples = sorted(glob.glob(os.path.join(SAMPLE_DIR, "*.jpg"))
                     + glob.glob(os.path.join(SAMPLE_DIR, "*.jpeg"))
                     + glob.glob(os.path.join(SAMPLE_DIR, "*.png")))
    if samples:
        return Image.open(random.choice(samples))
    return test_pattern(len(session_shots) + 1)


def mjpeg():
    while True:
        with stream.cond:
            stream.cond.wait()
            frame = stream.frame
        yield (b"--FRAME\r\nContent-Type: image/jpeg\r\n"
               b"Content-Length: " + str(len(frame)).encode() + b"\r\n\r\n"
               + frame + b"\r\n")


# ------------------------------------------------------------ fonts
def _font(size, bold=True):
    base = "/usr/share/fonts/truetype/dejavu/"
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    for p in [base + name,
              "/System/Library/Fonts/Supplemental/Courier New Bold.ttf"]:
        try:
            return ImageFont.truetype(p, size)
        except OSError:
            continue
    return ImageFont.load_default()


def centered(draw, y, text, font):
    box = draw.textbbox((0, 0), text, font=font)
    draw.text(((WIDTH - box[2]) // 2, y), text, font=font, fill=0)
    return y + box[3]


# ------------------------------------------------------------ imaging
def atkinson(img):
    """Cleaner whites/blacks than Floyd-Steinberg, but pure Python."""
    img = img.convert("L")
    px = img.load()
    w, h = img.size
    for y in range(h):
        for x in range(w):
            old = px[x, y]
            new = 255 if old > 127 else 0
            px[x, y] = new
            err = (old - new) // 8
            for dx, dy in ((1, 0), (2, 0), (-1, 1), (0, 1), (1, 1), (0, 2)):
                nx, ny = x + dx, y + dy
                if 0 <= nx < w and 0 <= ny < h:
                    px[nx, ny] = max(0, min(255, px[nx, ny] + err))
    return img.convert("1")


def crop_to_aspect(img, target):
    w, h = img.size
    if w / h > target:
        nw = int(h * target)
        return img.crop(((w - nw) // 2, 0, (w + nw) // 2, h))
    nh = int(w / target)
    return img.crop((0, (h - nh) // 2, w, (h + nh) // 2))


def process_frame(img, box_w, box_h):
    img = ImageOps.exif_transpose(img).convert("L")
    img = crop_to_aspect(img, box_w / box_h)
    img = img.resize((box_w, box_h), Image.LANCZOS)
    img = ImageOps.autocontrast(img, cutoff=2)
    img = ImageEnhance.Contrast(img).enhance(CONTRAST)
    img = img.filter(ImageFilter.UnsharpMask(*UNSHARP))
    img = img.point(lambda p: int(255 * (p / 255) ** GAMMA))
    return atkinson(img) if DITHER == "atkinson" else img.convert("1")


def _cute_font(size):
    """Chunky bubble face for the polaroid caption — same font as the UI."""
    for p in [os.path.join("static", "fonts", "TitanOne.ttf"),
              "/System/Library/Fonts/Supplemental/Bradley Hand Bold.ttf",
              "/System/Library/Fonts/Supplemental/Comic Sans MS.ttf"]:
        try:
            return ImageFont.truetype(p, size)
        except OSError:
            continue
    return _font(size)


def _logo(size):
    """1-bit stamp of the Hatchery logo: gold square goes solid black,
    the white H stays white — crisp on thermal paper."""
    try:
        img = Image.open(os.path.join("media", "hatchery_logo.png")).convert("L")
    except OSError:
        return None
    img = img.resize((size, size), Image.LANCZOS)
    return img.point(lambda p: 255 if p > 230 else 0).convert("1")


def compose_receipt(shots, mode):
    """Polaroid-style print: caption on top, wide white mat, photo(s), then a
    chin with the date, a small QR and the logo stamp. Mock render and real
    print share this."""
    n = len(shots)
    border = 38                          # polaroid mat width
    photo_w = WIDTH - 2 * border
    frame_h = STRIP_FRAME_H if mode == "strip" else SINGLE_FRAME_H
    frame_h = round(frame_h * photo_w / FRAME_W)   # keep aspect at new width

    f_caption = _cute_font(44)
    f_date    = _font(20, bold=False)
    f_handle  = _cute_font(30)
    f_tiny    = _font(15, bold=False)
    logo      = _logo(46)

    scratch = ImageDraw.Draw(Image.new("1", (WIDTH, 10), 1))
    cap_h    = scratch.textbbox((0, 0), CAPTION, font=f_caption)[3]
    date_h   = scratch.textbbox((0, 0), "0", font=f_date)[3]
    handle_b = scratch.textbbox((0, 0), HANDLE, font=f_handle)
    tiny_h   = scratch.textbbox((0, 0), "0", font=f_tiny)[3]
    row_h    = max(handle_b[3], logo.height if logo else 0)

    head_h = 26 + cap_h + 20
    chin_h = 26 + date_h + 20 + row_h + 10 + tiny_h + 26
    total_h = head_h + n * frame_h + GAP * (n - 1) + chin_h

    canvas = Image.new("1", (WIDTH, total_h), 1)
    d = ImageDraw.Draw(canvas)

    centered(d, 26, CAPTION, f_caption)

    y = head_h
    for shot in shots:
        canvas.paste(process_frame(shot, photo_w, frame_h), (border, y))
        d.rectangle([border - 1, y - 1, border + photo_w, y + frame_h],
                    outline=0, width=1)
        y += frame_h + GAP
    y -= GAP

    y += 26
    y = centered(d, y, datetime.datetime.now().strftime("%b %d, %Y").lower(),
                 f_date) + 20

    # logo + handle side by side, centred as one row
    row_gap = 16
    row_w = handle_b[2] + (logo.width + row_gap if logo else 0)
    x0 = (WIDTH - row_w) // 2
    if logo:
        canvas.paste(logo, (x0, y + (row_h - logo.height) // 2))
        x0 += logo.width + row_gap
    d.text((x0, y + (row_h - handle_b[3]) // 2), HANDLE, font=f_handle, fill=0)
    centered(d, y + row_h + 10, CREDIT, f_tiny)

    return canvas


def emit(bitmap):
    rid = uuid.uuid4().hex[:12]
    bitmap.save(os.path.join(RECEIPT_DIR, f"{rid}.png"))
    if PRINTER_ENABLED:
        from escpos.printer import Usb
        p = Usb(VENDOR_ID, PRODUCT_ID)
        p.image(bitmap, impl="bitImageRaster")
        p.text("\n\n")
        p.cut(mode="PART")      # leaves a tab so the receipt hangs in the slot
        p.close()
    return rid


# ------------------------------------------------------------ routes
@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/stream.mjpg")
def stream_route():
    if not CAMERA_ENABLED:
        return ("", 204)
    return Response(mjpeg(),
                    mimetype="multipart/x-mixed-replace; boundary=FRAME")


@app.route("/config")
def config():
    return jsonify(strip_shots=STRIP_SHOTS,
                   mock=not PRINTER_ENABLED,
                   camera=CAMERA_ENABLED)


@app.route("/reset", methods=["POST"])
def reset():
    session_shots.clear()
    return jsonify(ok=True)


@app.route("/capture", methods=["POST"])
def capture():
    try:
        session_shots.append(grab_frame())
        return jsonify(ok=True, count=len(session_shots))
    except Exception as e:
        traceback.print_exc()
        return jsonify(ok=False, error=str(e)), 500


@app.route("/undo", methods=["POST"])
def undo():
    if session_shots:
        session_shots.pop()
    return jsonify(ok=True, count=len(session_shots))


@app.route("/last.jpg")
def last_shot():
    if not session_shots:
        return ("", 404)
    img = ImageOps.exif_transpose(session_shots[-1]).convert("RGB")
    img.thumbnail((1200, 1200))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=85)
    return Response(buf.getvalue(), mimetype="image/jpeg")


@app.route("/print", methods=["POST"])
def do_print():
    t0 = time.time()
    try:
        mode = request.json.get("mode", "single")
        if not session_shots:
            return jsonify(ok=False, error="no shots"), 400
        rid = emit(compose_receipt(list(session_shots), mode))
        session_shots.clear()
        dt = round(time.time() - t0, 1)
        print(f"[{mode}] -> {rid} in {dt}s")
        return jsonify(ok=True, receipt=f"/receipts/{rid}.png",
                       mock=not PRINTER_ENABLED, seconds=dt)
    except Exception as e:
        traceback.print_exc()
        session_shots.clear()
        return jsonify(ok=False, error=str(e)), 500


if __name__ == "__main__":
    print("Booth |",
          "camera:", CAM_KIND.upper() if CAM_KIND else "SIMULATED",
          "| printer:", "REAL" if PRINTER_ENABLED else "MOCK",
          "| dither:", DITHER)
    if not CAMERA_ENABLED:
        n = len(glob.glob(os.path.join(SAMPLE_DIR, "*.jpg")) +
                glob.glob(os.path.join(SAMPLE_DIR, "*.jpeg")) +
                glob.glob(os.path.join(SAMPLE_DIR, "*.png")))
        print(f"       drop photos in ./{SAMPLE_DIR}/ to use as fake "
              f"captures (found {n})")
    app.run(host="127.0.0.1", port=8000, threaded=True)
