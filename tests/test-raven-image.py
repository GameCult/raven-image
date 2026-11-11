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
p, g = run("--margin", "6", "--size", "160x160", "--no-enhance", "cap")
out = open(os.path.join(tmp, "o.png"), "rb").read()
check(p.returncode == 0 and "cleared 0 of" in p.stdout and "backdrop #1ec85a" in p.stdout, f"clean band: exit 0, nothing cleared, backdrop reported ({p.stdout.strip()[-140:]})")
r_ = mod2.png_decode(out)[3]
check(all(bytes(r_[y][x * 3:x * 3 + 3]) == bytes(BG) for y in range(W) for x in range(W) if x < 16 or y < 16 or x >= W - 16 or y >= W - 16), "clean band: repainted exactly to the backdrop")
check(bytes(r_[80][80 * 3:80 * 3 + 3]) == bytes((250, 20, 20)), "clean band: interior untouched")
seen["view"] = mkpng(W, W, lambda x, y: (250, 20, 20) if (x < 12 and 30 <= y < 130) or (50 <= x < 110 and 50 <= y < 110) else BG)  # a stub entering the left band
p, g = run("--margin", "6", "--size", "160x160", "--no-enhance", "cap")
check(p.returncode == 65 and "DIRTY" in p.stdout and "left %d" % (12 * 100) in p.stdout and "top 0" in p.stdout, f"dirty band: exit 65 naming the side ({p.returncode} {p.stdout.strip()[-160:]})")
r_ = mod2.png_decode(open(os.path.join(tmp, "o.png"), "rb").read())[3]
check(all(bytes(r_[y][x * 3:x * 3 + 3]) == bytes(BG) for y in range(W) for x in range(12)), "dirty band: file is written and the stub cleared")
p, g = run("--margin", "6", "--max-clip", "50", "--size", "160x160", "--no-enhance", "cap")
check(p.returncode == 0, "--max-clip raises the tolerance")
holes = lambda x, y: (250, 20, 20) if (50 <= x < 110 and 50 <= y < 110 and not (70 <= x < 90 and 70 <= y < 90)) else BG  # ring around an enclosed backdrop hole
seen["view"] = mkpng(W, W, holes)
p, g = run("--margin", "6", "--alpha", "--size", "160x160", "--no-enhance", "cap")
w_, h_, b_, r_, k_ = mod2.png_decode(open(os.path.join(tmp, "o.png"), "rb").read())
check(p.returncode == 0 and b_ == 4 and r_[0][3] == 0 and r_[30][3 + 4 * 40] == 0 and r_[60][4 * 60 + 3] == 255, "--alpha: RGBA, outside keyed out, subject opaque")
check(r_[80][4 * 80 + 3] == 255, "--alpha: enclosed backdrop hole stays opaque (connected backdrop only)")
seen["view"] = PNG
p, g = run("--margin", "6", "--no-enhance", "cap")
check(p.returncode != 0 and "cannot read the render" in p.stderr and "Traceback" not in p.stderr, "undecodable render: clean error, no traceback")

srv.shutdown()
print(f"{count[0] - len(fails)}/{count[0]} checks passed")
sys.exit(1 if fails else 0)
