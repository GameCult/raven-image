#!/usr/bin/env python3
"""Offline test of client/raven-image against an in-process fake ComfyUI on 127.0.0.1.
Asserts on the POSTed /prompt graph and the multipart upload. Needs only python3. Usage: test-raven-image.py [repo-root]"""
import json, os, subprocess, sys, tempfile, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

root = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
tool = os.path.join(root, "client", "raven-image")
PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 32
seen = {"uploads": [], "prompts": [], "view": PNG}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def send(self, body, ctype="application/json"):
        self.send_response(200); self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    def do_GET(self):
        if self.path.startswith("/history/"):
            self.send(json.dumps({self.path[9:]: {"status": {"completed": True}, "outputs": {"9": {"images": [
                {"filename": "x.png", "subfolder": "", "type": "temp"}]}}}}).encode())
        elif self.path.startswith("/view"): self.send(seen["view"], "image/png")
        elif self.path == "/queue": self.send(b'{"queue_running":[],"queue_pending":[]}')
        else: self.send(b"{}")
    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        if self.path == "/upload/image":
            seen["uploads"].append(body); self.send(json.dumps({"name": "stored%d.png" % len(seen["uploads"])}).encode())
        elif self.path == "/prompt":
            seen["prompts"].append(json.loads(body)["prompt"]); self.send(b'{"prompt_id":"p1"}')
        else: self.send(b"{}")


srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
url = "http://127.0.0.1:%d" % srv.server_port
tmp = tempfile.mkdtemp(prefix="raven-image-test-")
fails = []
count = [0]


def check(cond, what):
    count[0] += 1
    if not cond:
        fails.append(what); print("FAIL:", what)


def run(*args, url=url):
    seen["uploads"].clear(); seen["prompts"].clear()
    env = dict(os.environ, RAVEN_URL=url)
    p = subprocess.run([sys.executable, "-I", tool, "--keep-loaded", "-o", os.path.join(tmp, "o.png"), *args],
                       capture_output=True, text=True, env=env, timeout=120)
    return p, (seen["prompts"][0] if seen["prompts"] else None)


def img(name, data=PNG):
    p = os.path.join(tmp, name)
    open(p, "wb").write(data)
    return p


refs = [img("r%d.png" % i) for i in range(4)]
tmpl = json.load(open(os.path.join(root, "client", "graphs", "flux3-klein-edit.json")))

# ref counts 1, 2, 4
for n in (1, 2, 4):
    a = []
    for r in refs[:n]: a += ["--ref", r]
    p, g = run(*a, "cap <ref_image_0>")
    check(p.returncode == 0 and g is not None, f"{n} refs: rendered ({p.stderr[-200:]})")
    if not g: continue
    check(len(seen["uploads"]) == n, f"{n} refs: {n} uploads")
    keys = ["12", "21", "22", "23"][:n]
    check(all(g[k]["class_type"] == "LoadImage" for k in keys), f"{n} refs: LoadImage nodes {keys}")
    check(sorted(k for k in g if k in ("12", "21", "22", "23")) == sorted(keys), f"{n} refs: no extra LoadImage")
    check(g["13"]["inputs"] == dict(tmpl["13"]["inputs"], **{f"images.image{i}": [k, 0] for i, k in enumerate(keys)}), f"{n} refs: node 13 images")
    check(g["14"]["inputs"] == {f"images.image{i}": [k, 0] for i, k in enumerate(keys)}, f"{n} refs: node 14 images")
    check(g["4"]["class_type"] == "CLIPTextEncode" and g["4"]["inputs"]["text"] == ["11", 0], f"{n} refs: node 4 reads the upsampler")
    check(g["11"]["inputs"]["prompt"] == "cap <ref_image_0>", f"{n} refs: node 11 prompt is the caption")
    check(g["12"]["inputs"]["image"] == "stored1.png", f"{n} refs: ref 0 uses the stored name")

# --vision
p, g = run("--ref", refs[0], "--ref", refs[1], "--ref", refs[2], "--ref", refs[3], "--vision", "cap")
check(p.returncode == 0, "vision: rendered")
if g:
    e = g["4"]
    check(e["class_type"] == "TextEncodeQwenImageEditPlus", "vision: node 4 is the vision encoder")
    check(e["inputs"] == {"clip": ["2", 0], "prompt": ["11", 0], "image1": ["12", 0], "image2": ["21", 0], "image3": ["22", 0]},
          "vision: encoder gets the first 3 refs and the upsampler link")
# vision + no-enhance: one authority for node 4
p, g = run("--ref", refs[0], "--vision", "--no-enhance", "plain cap")
check(p.returncode == 0, "vision+no-enhance: rendered")
if g:
    check(g["4"]["inputs"] == {"clip": ["2", 0], "prompt": "plain cap", "image1": ["12", 0]}, "vision+no-enhance: node 4 has no stale text key")
    check("10" not in g and "11" not in g and "14" not in g, "vision+no-enhance: upsampler nodes removed")
# no-enhance, t2i and edit
p, g = run("--no-enhance", "plain cap")
check(g is not None and g["4"]["inputs"]["text"] == "plain cap" and "11" not in g, "no-enhance t2i: caption in node 4, upsampler gone")
p, g = run("--ref", refs[0], "--no-enhance", "plain cap")
check(g is not None and g["4"]["inputs"]["text"] == "plain cap" and "14" not in g and "11" not in g, "no-enhance edit: caption in node 4")

# hostile prompt stays inside node 11's prompt; no shell execution
marker = os.path.join(tmp, "pwned")
hostile = 'q"\' \\ \\" \n\r\t\x01\x1b {"6":1} $(touch %s) `touch %s` ; ☃ ‮ 😀'.replace("😀", "\U0001F600") % (marker, marker)
for extra in ([], ["--ref", refs[0]]):
    p, g = run(*extra, hostile)
    base = json.load(open(os.path.join(root, "client", "graphs", "flux3-klein.json" if not extra else "flux3-klein-edit.json")))
    check(g is not None and hostile in g["11"]["inputs"]["prompt"], "hostile: prompt verbatim in node 11")
    if g:
        same = all(g[k] == base[k] for k in base if k not in ("6", "7", "11", "13", "14", "12"))
        check(same, "hostile: every other node untouched")
        check(g["6"]["inputs"]["width"] == 1536 and g["6"]["inputs"]["batch_size"] == 1, "hostile: node 6 untouched")
check(not os.path.exists(marker), "hostile: nothing executed")
check("subprocess" not in open(tool).read() and "os.system" not in open(tool).read(), "tool never shells out")

# argument errors, each before any upload
def refused(args, needle, what):
    p, g = run(*args)
    check(p.returncode != 0 and g is None and not seen["uploads"], f"{what}: refused with no upload/prompt")
    check("Traceback" not in p.stderr and needle in p.stderr, f"{what}: clean message ({p.stderr.strip()[-160:]})")
refused(["--vision", "cap"], "--vision needs", "--vision without --ref")
refused(["--ref", refs[0], "--graph", "flux3-klein", "cap"], "go together", "--ref with t2i graph")
refused(["--graph", "flux3-klein-edit", "cap"], "go together", "edit graph without --ref")
refused(["--ref", refs[0], "--ref", "no/such/file.png", "cap"], "--ref 'no/such/file.png': no such file", "missing ref")
check(tmp not in run("--ref", "no/such/file.png", "cap")[0].stderr, "missing ref: error shows the path as typed, not resolved")
refused(["--ref", refs[0], "--ref", img("t.txt", b"hello"), "cap"], "not a png, jpeg or webp", "non-image ref")
refused(["--ref", img("big.png", PNG + b"\0" * (20 * 2 ** 20)), "cap"], "larger than 20 MiB", "oversize ref")
refused(["--ref", "/dev/null", "cap"], "not a regular file", "device ref")
os.symlink("/etc/passwd", os.path.join(tmp, "link.png"))
refused(["--ref", os.path.join(tmp, "link.png"), "cap"], "not a png, jpeg or webp", "symlink to non-image")
refused(["--ref", tmp, "cap"], "not a regular file", "directory ref")
# a FIFO as --ref (nobody ever writes to it) is refused and must not hang. run() has no tight timeout, so guard with SIGALRM.
import importlib.machinery, importlib.util, signal
fifo = os.path.join(tmp, "swap.png")
os.mkfifo(fifo)
class Hung(Exception): pass
def _alarm(*a): raise Hung()
signal.signal(signal.SIGALRM, _alarm)
try:
    signal.alarm(10)
    refused(["--ref", fifo, "cap"], "not a regular file", "fifo ref")
except (Hung, subprocess.TimeoutExpired):
    check(False, "fifo ref: refused without hanging")
finally:
    signal.alarm(0)
# the swap itself: the path looks like a regular file to every earlier check, then is a FIFO when opened
mod = importlib.util.module_from_spec(importlib.util.spec_from_loader("ri", importlib.machinery.SourceFileLoader("ri", tool)))
mod.__spec__.loader.exec_module(mod)
mod.os.path.exists = mod.os.path.isfile = lambda p: True
try:
    signal.alarm(10)
    try:
        mod.load_ref(fifo)
        check(False, "fifo swap: refused")
    except SystemExit as e:
        check("not a regular file" in str(e), f"fifo swap: refused by fstat on the opened fd ({e})")
except Hung:
    check(False, "fifo swap: refused without hanging")
finally:
    signal.alarm(0)
# accepted types
for nm, data in (("a.jpg", b"\xff\xd8\xff\xe0" + b"\0" * 8), ("a.webp", b"RIFF\0\0\0\0WEBPVP8 ")):
    p, g = run("--ref", img(nm, data), "cap")
    check(p.returncode == 0, f"{nm} accepted")

# multipart filename: hostile names are sanitised
for nm in ('a"b\r\nX-Evil: 1\\c.png', "pass wd;$x.png", "é.png"):
    p, g = run("--ref", img(nm, PNG), "cap")
    hdr = seen["uploads"][0].split(b"\r\n\r\n", 1)[0].decode() if seen["uploads"] else ""
    lines = hdr.split("\r\n")
    check(len(lines) == 3 and lines[1].startswith('Content-Disposition: form-data; name="image"; filename="raven-image-') and lines[1].endswith('"')
          and lines[1].count('"') == 4 and "X-Evil" not in "".join(lines[2:]), f"filename {nm!r}: header intact ({lines})")
    fn = lines[1].split('filename="')[1][:-1] if len(lines) > 1 else ""
    check(fn and all(c.isascii() and (c.isalnum() or c in "._-") for c in fn), f"filename {nm!r}: charset safe ({fn})")

# unreachable Raven: 75 with the hedged message
p, _ = run("cap", url="http://127.0.0.1:1")
check(p.returncode == 75 and "may be busy or off" in p.stderr and "lock screen" in p.stderr, "unreachable: exit 75, hedged message names lock screen")

# --margin: the pin is in the graph, the band is measured and repainted, a dirty band is loud
import importlib.machinery, importlib.util, struct, zlib
mod2 = importlib.util.module_from_spec(importlib.util.spec_from_loader("ri2", importlib.machinery.SourceFileLoader("ri2", tool)))
mod2.__spec__.loader.exec_module(mod2)
def mkpng(w, h, px, bpp=3, filt=0):
    """px(x, y) -> tuple of bpp bytes. filt 0..4 applies that PNG filter to every row (the decoder must undo all five)."""
    rows = [bytes(v for x in range(w) for v in px(x, y)) for y in range(h)]
    raw, prev = b"", bytes(w * bpp)
    for r in rows:
        f = bytearray()
        for i in range(w * bpp):
            a, b, c = (r[i - bpp] if i >= bpp else 0), prev[i], (prev[i - bpp] if i >= bpp else 0)
            pr = (0, a, b, (a + b) >> 1, (lambda pa, pb, pc: a if pa <= pb and pa <= pc else b if pb <= pc else c)(abs(b - c), abs(a - c), abs(a + b - 2 * c)))[filt]
            f.append((r[i] - pr) & 255)
        raw += bytes([filt]) + bytes(f); prev = r
    ck = lambda t, b: struct.pack(">I", len(b)) + t + b + struct.pack(">I", zlib.crc32(t + b))
    return (b"\x89PNG\r\n\x1a\n" + ck(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2 if bpp == 3 else 6, 0, 0, 0))
            + ck(b"tEXt", b"prompt\0keepme") + ck(b"IDAT", zlib.compress(raw)) + ck(b"IEND", b""))
BG = (30, 200, 90)
# codec: every filter type round-trips, ancillary chunks survive
for f in range(5):
    for bpp in (3, 4):
        pic = lambda x, y, bpp=bpp: ((x * 7 + y * 3) & 255, (x * y) & 255, (x ^ y) & 255) + ((200,) if bpp == 4 else ())
        w_, h_, b_, rows_, keep_ = mod2.png_decode(mkpng(37, 23, pic, bpp, f))
        check((w_, h_, b_) == (37, 23, bpp) and all(bytes(rows_[y][x * bpp:(x + 1) * bpp]) == bytes(pic(x, y)) for y in range(23) for x in range(37))
              and keep_ == [(b"tEXt", b"prompt\0keepme")], f"png codec: filter {f}, {bpp} bytes/px")
        check(mod2.png_decode(mod2.png_encode(w_, h_, b_, rows_, keep_))[3] == rows_, f"png codec: encode round-trips (filter {f}, {bpp})")
for bad in (b"nope", mkpng(8, 8, lambda x, y: BG)[:40]):
    try: mod2.png_decode(bad); check(False, "png_decode refuses garbage")
    except (ValueError, zlib.error, struct.error): check(True, "")
# margin_px: pct of each axis, up to the 16 px latent grid
check(mod2.margin_px(6, 1024, 1024) == (64, 64) and mod2.margin_px(5, 1536, 1024) == (80, 64) and mod2.margin_px(0.1, 1024, 1024) == (16, 16),
      "margin_px: 16 px steps per axis")
# the pin graph: flat-bg latent, noise mask 0 on the band and 1 inside, sampler starts from it
for extra, base in (([], "flux3-klein.json"), (["--ref", refs[0]], "flux3-klein-edit.json")):
    p, g = run(*extra, "--margin", "5", "--bg", "#12fa5f", "--size", "1536x1024", "--no-enhance", "cap")
    check(g is not None and "6" not in g and g["7"]["inputs"]["latent_image"] == ["35", 0], f"margin {base}: sampler starts from the masked latent, empty latent gone")
    if g:
        check(g["30"]["inputs"] == {"width": 1536, "height": 1024, "batch_size": 1, "color": 0x12fa5f}, f"margin {base}: flat bg image at full size")
        check(g["31"] == {"class_type": "VAEEncode", "inputs": {"pixels": ["30", 0], "vae": ["3", 0]}}, f"margin {base}: bg encoded with the graph's vae")
        check(g["32"]["inputs"] == {"value": 0.0, "width": 1536, "height": 1024} and g["33"]["inputs"] == {"value": 1.0, "width": 1536 - 160, "height": 1024 - 128},
              f"margin {base}: mask 0 outside, 1 inside by 80x64 px")
        check(g["34"]["inputs"]["x"] == 80 and g["34"]["inputs"]["y"] == 64 and g["34"]["inputs"]["operation"] == "add"
              and g["34"]["inputs"]["destination"] == ["32", 0] and g["34"]["inputs"]["source"] == ["33", 0], f"margin {base}: interior composited at the margin offset")
        check(g["35"]["inputs"] == {"samples": ["31", 0], "mask": ["34", 0]} and g["7"]["inputs"]["denoise"] == 1, f"margin {base}: noise mask on the encoded bg, denoise stays 1")
p, g = run("--no-enhance", "cap")
check(g is not None and "6" in g and "35" not in g, "no --margin: graph unchanged")
for args, needle in ((["--margin", "0"], "--margin is"), (["--margin", "41"], "--margin is"), (["--margin", "5", "--bg", "green"], "--bg is"), (["--alpha"], "--alpha needs --margin")):
    refused([*args, "cap"], needle, " ".join(args))
# the render is measured: clean band exits 0 and is exact, content in the band is cleared and exits 65, alpha keys only connected backdrop
W = 160  # 6% -> 16 px band
def scene(x, y, hole=False):
    if x < 6 and y < 6 or (hole and 80 <= x < 90 and 80 <= y < 90): return (250, 20, 20)  # corner speck in the band; an enclosed hole is separate below
    return BG if not (50 <= x < 110 and 50 <= y < 110) else (250, 20, 20)
seen["view"] = mkpng(W, W, lambda x, y: (BG[0] + (x + y) % 3, BG[1], BG[2]) if not (50 <= x < 110 and 50 <= y < 110) else (250, 20, 20))
p, g = run("--margin", "6", "--bg", "1ec85a", "--size", "160x160", "--no-enhance", "cap")
out = open(os.path.join(tmp, "o.png"), "rb").read()
check(p.returncode == 0 and "cleared 0 of" in p.stdout and "backdrop #1ec85a" in p.stdout, f"clean band: exit 0, nothing cleared, backdrop reported ({p.stdout.strip()[-140:]})")
r_ = mod2.png_decode(out)[3]
check(all(bytes(r_[y][x * 3:x * 3 + 3]) == bytes(BG) for y in range(W) for x in range(W) if x < 16 or y < 16 or x >= W - 16 or y >= W - 16), "clean band: repainted exactly to the backdrop")
check(bytes(r_[80][80 * 3:80 * 3 + 3]) == bytes((250, 20, 20)), "clean band: interior untouched")
seen["view"] = mkpng(W, W, lambda x, y: (250, 20, 20) if (x < 12 and 30 <= y < 130) or (50 <= x < 110 and 50 <= y < 110) else BG)  # a stub entering the left band
p, g = run("--margin", "6", "--bg", "1ec85a", "--size", "160x160", "--no-enhance", "cap")
check(p.returncode == 65 and "DIRTY" in p.stdout and "left %d" % (12 * 100) in p.stdout and "top 0" in p.stdout, f"dirty band: exit 65 naming the side ({p.returncode} {p.stdout.strip()[-160:]})")
r_ = mod2.png_decode(open(os.path.join(tmp, "o.png"), "rb").read())[3]
check(all(bytes(r_[y][x * 3:x * 3 + 3]) == bytes(BG) for y in range(W) for x in range(12)), "dirty band: file is written and the stub cleared")
p, g = run("--margin", "6", "--bg", "1ec85a", "--max-clip", "50", "--size", "160x160", "--no-enhance", "cap")
check(p.returncode == 0, "--max-clip raises the tolerance")
holes = lambda x, y: (250, 20, 20) if (50 <= x < 110 and 50 <= y < 110 and not (70 <= x < 90 and 70 <= y < 90)) else BG  # ring around an enclosed backdrop hole
seen["view"] = mkpng(W, W, holes)
p, g = run("--margin", "6", "--bg", "1ec85a", "--alpha", "--size", "160x160", "--no-enhance", "cap")
w_, h_, b_, r_, k_ = mod2.png_decode(open(os.path.join(tmp, "o.png"), "rb").read())
check(p.returncode == 0 and b_ == 4 and r_[0][3] == 0 and r_[30][3 + 4 * 40] == 0 and r_[60][4 * 60 + 3] == 255, "--alpha: RGBA, outside keyed out, subject opaque")
check(r_[80][4 * 80 + 3] == 255, "--alpha: enclosed backdrop hole stays opaque (connected backdrop only)")
seen["view"] = PNG
p, g = run("--margin", "6", "--no-enhance", "cap")
check(p.returncode != 0 and "cannot read the render" in p.stderr and "Traceback" not in p.stderr, "undecodable render: clean error, no traceback")

PIL_FIXTURES = [  # (PNG written by Pillow, optimize=True: its encoder picks filters 1, 2, 3 and 4; sha256 of the raw RGB it was made from)
    ("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",
     "8b888b5e56272e9532dce8baab527bd9ac1c6351a8db13171036aaa50c785999"),
    ("iVBORw0KGgoAAAANSUhEUgAAADAAAAAgCAIAAADbtmxLAAAEwElEQVR42m1WW5LjOAwDCE46nelD78EtcT9kKlB6Uq6ULVEk+AJF4D8IEKiAimIIVIRGSKErlNIIhYQUpZmKVKWYmn80U/ijeIh/VA/xITzEh8aX9ND4Ep7Cl+opPsWn6il8S0/hW/Ol+BZeGi/FS3iJSQQQwCQABDGJIIoQMQj1CgkQkyBRRBE810EUbw0gSExCBM5nnR1E8DBXS3kSE5hAEhcQjaOItPPVEJexIECMFoC9XCZQLSPiIkSQGO3DMEn0O5MEGGCRCYK8yCCDBAmSZJFFBjlIkCInKRJktcwki2SfCrLISSY5Wufg/VsHl54gr7YS5EhGICYiGQMhRjImoxi4gUUxyJgMMWC7V++ihauFL0Yy1J/7BYxo4SWDXly7TMZECDEZhduMY5p9YDBGq0Pv7s8N92IUIxhX785egfkGhhij4S7Nk4FkAEFEMYQYfGMqxmCIEYxiqBWxHZKBW4iXmWVya8jWuR3bCMpSsc7OsA3uuLWlbawsfUvFMI93tMpkdo6q4xftzOwkLltpnxHUpIoKChQpUkVd/bnWgxrUpIISNVtyrQdFalLoJ6iiQGULzzY0KFKgJpXUBrAUIpmAJpTUxI2s7GS1jaVlNKYFOlpX9eIwr7aZDfoD62WIl+czmQld0KQm3mdGi44GJAvA7Gh5kKojPdt29YNzq1oJTwdAMZkTyh4ds0XVL2HhqX4hJdO1w46W3+C2Y9WSNFiOe3ykrKjAvT3N/MrFZfG4WpGbX+/rcwlka7goGYLtpDplRwaSfwoKiBRxVwksF9Wfs8tZZ47KMJWVy/UryzqdubooZcUUyceKECngjts+WW2+qDQc2f+uEdZ9syXDuq8shNlRqW635dJIPgKaUFAX3qGj/S97wxoH1r06TdK6Wmf9bkDTXPWGnRRXhAhNKvFucrTEtD4PS1+aOp4FO63+omMQ5gaNt4Z5vrrsQahWOeO2sStfdgZGNrNLeHYkdmpkbR+nPzwzOI2TtGOW/Crc5QzcG2V8nWc7XHYeXUnzhBhnfw2Lx0Y2LCpb/6Ai+RzNQxduJzaLuLvbJx8F2ZbibOl58uFouNfJsWV0sPIQySegC8q+U//uZBo7O32n8YKMA6PtfXDM+MXsH7W4eOgpI8ZdYo56z8idac8LWlJn59cZAydS2uMH46WZfC5iHLwTtznwg92nMcJuk48oOtw4y9Zxb7oPu1ncvJX8BgSIPe0365cN6tHDOf5F02Gh2oPCyamMtGhJLEv9cmAkX7C2T+tz/SvUYdFyWBto/poPabxXFuAdmKv/k5rJFyGsOY/3ZN1ysM/8Naidcz9GXlln+brTo3duUBdfkXyttk9q4J2Caep8XqaNOY8Zu/s+LnTTbgGD75IYdg8hXxsfki9B1dO+bFLipFf+4g9YJ+qMzR7Jcd4Cdn8UFXzB7jb3/E7+XUw9eMMaJ/HXOaU35VznLWUYGhidemeBEv+WFXVYVVUT409ABYk98zsv1Zmq7rK0mJXZ4znsnPfa+586gwGb9rLLUyV/BgQoerimcS6tCIbdhMJ6hBabkyx+nJ/CiH5rkM049Cz7ITShyTtOe47WWUzOHDhnyC7z5M9uomFzbZMNrOT9nj4pvCSq/gfyN3gTGCrIuQAAAABJRU5ErkJggg==",
     "39c1118e88d437671a8d8474eeb749c12fc3bbc7baceaaf3ee7198308ef95a3b"),
    ("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",
     "f55679ae730350261f805f3db051d1dc4dec58bac8d5dafc880ee51cc983d9ee"),
]

import base64, hashlib
for i, (b64, sha) in enumerate(PIL_FIXTURES):
    w_, h_, b_, r_, k_ = mod2.png_decode(base64.b64decode(b64))
    check(hashlib.sha256(b"".join(bytes(r) for r in r_)).hexdigest() == sha, f"png codec: Pillow-written fixture {i} decodes to the pixels it was made from (independent encoder, filters 1-4)")
# exact counts on a synthetic band: corners belong to top/bottom, TOL is inclusive, the denominator is the band
def sq(x0, y0, x1, y1, c): return lambda x, y: c if x0 <= x < x1 and y0 <= y < y1 else None
def compose(*shapes, bg=BG):
    return lambda x, y: next((v for v in (s_(x, y) for s_ in shapes) if v), bg)
RED = (250, 20, 20); NEAR = (BG[0] + 12, BG[1], BG[2]); FAR = (BG[0] + 13, BG[1], BG[2])
scene2 = compose(sq(0, 0, 5, 5, RED), sq(2, 60, 5, 70, RED), sq(156, 60, 160, 63, RED), sq(158, 158, 160, 160, RED),
                 sq(4, 100, 6, 102, NEAR), sq(4, 110, 7, 111, FAR), sq(144, 80, 145, 82, RED), sq(15, 90, 16, 92, RED), sq(16, 90, 17, 92, RED))
png2, back2, clip2, band2 = mod2.clear_edges(mkpng(160, 160, scene2), 6, False, 0x1ec85a)
check(band2 == 160 * 160 - 128 * 128 and back2 == BG, f"clear_edges: band is {band2}, backdrop {back2}")
check(clip2 == {"left": 30 + 3 + 2, "top": 25, "right": 12 + 2, "bottom": 4}, f"clear_edges: exact per-side counts, corners to top/bottom, distance 12 clean and 13 content ({clip2})")
check(b"tEXt" in png2, "clear_edges: ancillary chunks survive the re-encode")
# a band that is mostly content is not a backdrop: it is dirty against --bg, never promoted
for frac, label in ((1.0, "all content"), (0.55, "55% content")):
    pic = lambda x, y, f=frac: RED if (x < 16 or y < 16 or x >= 144 or y >= 144) and (x * 7 + y * 13) % 100 < f * 100 else BG
    _, back3, clip3, band3 = mod2.clear_edges(mkpng(160, 160, pic), 6, False, 0x1ec85a)
    check(100 * sum(clip3.values()) / band3 > 0.5, f"band that is {label}: reported dirty ({sum(clip3.values())}/{band3})")
# flood fill reaches through 1-px corridors in both axes
pocket_v = compose(sq(80, 20, 81, 80, BG), sq(75, 80, 86, 100, BG), sq(50, 50, 110, 110, RED))   # vertical corridor into a pocket
pocket_h = compose(sq(20, 80, 80, 81, BG), sq(80, 75, 100, 86, BG), sq(50, 50, 110, 110, RED))   # horizontal corridor into a pocket
for nm, sc, probe in (("vertical", pocket_v, (80, 90)), ("horizontal", pocket_h, (90, 80))):
    pic = mkpng(160, 160, sc)
    _, _, bpp_, rows_, _ = mod2.png_decode(pic)
    w2, h2, bpp2, rows2, keep2 = mod2.png_decode(mod2.clear_edges(pic, 6, True, 0x1ec85a)[0])
    check(rows2[probe[1]][probe[0] * 4 + 3] == 0, f"flood fill: backdrop pocket behind a 1-px {nm} corridor is keyed out")
# BG_TOL: a flat band within 48 levels of --bg is the pinned colour after the VAE's shift (observed up to 30), repainted as the band's own colour; 49 is content
for off, want_clean in ((48, True), (49, False)):
    flat = (BG[0] + off, BG[1], BG[2])
    p_, back4, clip4, band4 = mod2.clear_edges(mkpng(160, 160, lambda x, y, f=flat: f), 6, False, 0x1ec85a)
    check((sum(clip4.values()) == 0) == want_clean and back4 == (flat if want_clean else BG), f"BG_TOL: flat band {off} levels off --bg {'is' if want_clean else 'is not'} a backdrop ({back4}, {sum(clip4.values())} cleared)")
    if want_clean:
        r4 = mod2.png_decode(p_)[3]
        check(bytes(r4[3][3:6]) == bytes(flat), "BG_TOL: a clean band is repainted with its own colour, so the seam stays invisible")
# decoder hardening: a flipped byte breaks the CRC, and every size check refuses on its own (each blob is otherwise well-formed)
def ck(t, b): return struct.pack(">I", len(b)) + t + b + struct.pack(">I", zlib.crc32(t + b))
def blob(w, h, depth=8, ctype=2, idat=None, il=0):
    return (b"\x89PNG\r\n\x1a\n" + ck(b"IHDR", struct.pack(">IIBBBBB", w, h, depth, ctype, 0, 0, il))
            + ck(b"IDAT", idat if idat is not None else zlib.compress(b"\0" * (h * (w * 3 + 1)) if w * h < 10 ** 7 else b"")) + ck(b"IEND", b""))
good = mkpng(8, 8, lambda x, y: BG)
bad = bytearray(good); bad[40] ^= 1
for data, what in ((bytes(bad), "flipped byte (CRC)"), (blob(2 ** 21, 2 ** 21), "a header that promises far more than the data"), (blob(2 ** 32 - 1, 2 ** 32 - 1), "a 2**32-1 square header (must refuse before allocating)"), (blob(8, 8, idat=zlib.compress(b"\0" * (8 * 25 - 1))), "image data shorter than the header says"),
                   (blob(8, 8, idat=zlib.compress(b"\0" * (8 * 25 + 500))), "image data longer than the header says"), (blob(8, 8, depth=16), "16-bit"),
                   (blob(8, 8, ctype=3), "palette"), (blob(8, 8, il=1), "interlaced"), (good[:-20], "truncated"),
                   (good[:8] + struct.pack(">I", 10 ** 6) + good[12:], "chunk length past the end")):
    try: mod2.png_decode(data); check(False, f"png_decode refuses {what}")
    except (ValueError, zlib.error, struct.error): check(True, "")
    except BaseException as e: check(False, f"png_decode refuses {what} cleanly, got {type(e).__name__}")
# size and interior validation, before any request
refused(["--size", "foo", "cap"], "--size is WxH", "--size foo")
refused(["--size", "1000x1024", "cap"], "multiples of 16", "--size not on the grid")
refused(["--margin", "40", "--size", "64x64", "cap"], "leaves no room", "margin eating the interior")
refused(["--margin", "5", "--size", "32x512", "cap"], "leaves no room", "margin eating one axis only")
# -n: a dirty image is not forgotten by a later one; an unreadable render is reported, not fatal to the batch
seen["view"] = mkpng(160, 160, lambda x, y: RED if x < 12 else BG)
p, g = run("--margin", "6", "--bg", "1ec85a", "-n", "2", "--size", "160x160", "--no-enhance", "cap")
check(p.returncode == 65 and p.stdout.count("DIRTY") == 2, f"-n 2 both dirty: two reports, exit 65 ({p.returncode})")

srv.shutdown()
print(f"{count[0] - len(fails)}/{count[0]} checks passed")
sys.exit(1 if fails else 0)
