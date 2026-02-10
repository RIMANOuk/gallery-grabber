import os
import re
import time
import uuid
import tempfile
import zipfile
from urllib.parse import urljoin, urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from bs4 import BeautifulSoup
from fastapi import FastAPI, Form, HTTPException
from fastapi.responses import HTMLResponse, FileResponse

app = FastAPI()

# Support more formats than just jpg/jpeg
IMG_RE = re.compile(r"\.(jpe?g|png|webp|gif|svg|avif)(\?.*)?$", re.IGNORECASE)

# In-memory cache of results: token -> {created, url, name, image_urls}
# Good enough for personal use. If the service restarts, tokens are lost.
RESULTS = {}
TOKEN_TTL_SECONDS = 15 * 60  # 15 minutes

PAGE = """
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Gallery Grabber</title>
<style>
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif; margin: 40px; max-width: 980px; }
h1 { margin: 0 0 6px; font-size: 32px; }
p { margin: 0 0 18px; color: #444; }
.card { background: #f6f6f7; border: 1px solid #e6e6ea; border-radius: 14px; padding: 18px; }
label { display: block; font-weight: 600; margin: 10px 0 8px; }
input[type=text] { width: 100%; padding: 12px; font-size: 16px; border-radius: 10px; border: 1px solid #ccc; box-sizing: border-box; }
.row { display: flex; gap: 10px; margin-top: 14px; align-items: center; }
button { padding: 12px 16px; font-size: 16px; border-radius: 10px; border: 1px solid #111; background: #111; color: #fff; cursor: pointer; }
button:disabled { opacity: .6; cursor: not-allowed; }
.small { font-size: 12px; color: #777; margin-top: 14px; }
.footer { margin-top: 18px; font-size: 12px; color: #777; }
.spinner { width: 16px; height: 16px; border: 2px solid #ddd; border-top: 2px solid #111; border-radius: 50%; display: none; animation: spin .9s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }
.status { font-size: 14px; display:none; color: #333; }
</style>
</head>
<body>

<h1>Gallery Grabber</h1>
<p>Paste a page URL. It will find images on that page and show you a results list before you download the ZIP.</p>

<div class="card">
<form id="form" method="post" action="/preview">
  <label>Page URL</label>
  <input name="url" type="text" required placeholder="https://example.com/page-or-gallery">

  <label>ZIP name (optional)</label>
  <input name="name" type="text" placeholder="event-name">

  <div class="row">
    <button id="btn" type="submit">Find images</button>
    <div class="spinner" id="spin"></div>
    <div class="status" id="status">Fetching and scanning…</div>
  </div>

  <div class="small">Tool by RIMANO</div>
</form>
</div>

<script>
const form = document.getElementById("form");
const btn = document.getElementById("btn");
const spin = document.getElementById("spin");
const status = document.getElementById("status");

form.addEventListener("submit", () => {
  btn.disabled = true;
  spin.style.display = "inline-block";
  status.style.display = "inline-block";
});
</script>

</body>
</html>
"""

def build_session():
    s = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=0.8,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "HEAD"]
    )
    adapter = HTTPAdapter(max_retries=retry)
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-GB,en;q=0.9",
        "Connection": "keep-alive",
    })
    return s

def safe_zip_name(name: str) -> str:
    name = (name or "").strip()
    if not name:
        return "gallery"
    name = re.sub(r"[^a-zA-Z0-9._-]+", "-", name).strip("-")
    return name[:80] or "gallery"

def default_zip_name_from_url(url: str) -> str:
    try:
        p = urlparse(url)
        host = (p.netloc or "gallery").replace("www.", "")
        path = (p.path or "").strip("/")
        if not path:
            return host
        seg = path.split("/")[-1] or "page"
        return f"{host}-{seg}"
    except Exception:
        return "gallery"

def extract_image_urls(page_url: str, html: str):
    soup = BeautifulSoup(html, "html.parser")
    found = []

    def add(u):
        if not u:
            return
        u = str(u).strip()
        if not u or u.startswith("data:"):
            return
        abs_u = urljoin(page_url, u)
        if IMG_RE.search(abs_u):
            found.append(abs_u)

    # <a href="...jpg/png/etc">
    for a in soup.select("a[href]"):
        add(a.get("href", ""))

    # <img> tags: src, srcset, and common lazy-load attrs
    for img in soup.select("img"):
        add(img.get("src", ""))

        # srcset: pick the largest
        srcset = img.get("srcset", "") or ""
        if srcset:
            best_url = None
            best_w = -1
            for part in srcset.split(","):
                part = part.strip()
                if not part:
                    continue
                bits = part.split()
                url = bits[0]
                score = 0
                if len(bits) > 1:
                    token = bits[1]
                    if token.endswith("w"):
                        try: score = int(token[:-1])
                        except: score = 0
                    elif token.endswith("x"):
                        try: score = int(float(token[:-1]) * 1000)
                        except: score = 0
                if score > best_w:
                    best_w = score
                    best_url = url
            if best_url:
                add(best_url)

        for attr in ["data-src", "data-lazy-src", "data-original", "data-image", "data-url"]:
            add(img.get(attr, ""))

        for attr in ["data-srcset", "data-lazy-srcset"]:
            val = img.get(attr, "") or ""
            if val:
                # treat like srcset: pick largest
                best_url = None
                best_w = -1
                for part in val.split(","):
                    part = part.strip()
                    if not part:
                        continue
                    bits = part.split()
                    url = bits[0]
                    w = 0
                    if len(bits) > 1 and bits[1].endswith("w"):
                        try: w = int(bits[1][:-1])
                        except: w = 0
                    if w > best_w:
                        best_w = w
                        best_url = url
                if best_url:
                    add(best_url)

    # OG/Twitter image
    for meta in soup.select('meta[property="og:image"], meta[name="twitter:image"]'):
        add(meta.get("content", ""))

    # Icons
    for link in soup.select('link[rel~="icon"], link[rel="apple-touch-icon"], link[rel="apple-touch-icon-precomposed"]'):
        add(link.get("href", ""))

    # Inline CSS background-image and other url(...) occurrences
    for el in soup.select("[style]"):
        style = el.get("style", "") or ""
        for m in re.finditer(r"url\(([^)]+)\)", style, re.IGNORECASE):
            raw = m.group(1).strip().strip('"').strip("'")
            add(raw)

    # De-dupe while keeping order
    return list(dict.fromkeys(found))

def cleanup_old_results():
    now = time.time()
    expired = [t for t, v in RESULTS.items() if now - v["created"] > TOKEN_TTL_SECONDS]
    for t in expired:
        RESULTS.pop(t, None)

@app.get("/", response_class=HTMLResponse)
def home():
    return PAGE

@app.get("/health")
def health():
    return {"ok": True}

@app.post("/preview", response_class=HTMLResponse)
def preview(url: str = Form(...), name: str = Form("")):
    cleanup_old_results()
    s = build_session()

    try:
        page = s.get(url, timeout=30, allow_redirects=True)
        page.raise_for_status()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not fetch page: {e}")

    images = extract_image_urls(url, page.text)

    # Build token + store results
    token = uuid.uuid4().hex
    final_name = (name or "").strip() or default_zip_name_from_url(url)
    final_name = safe_zip_name(final_name)

    RESULTS[token] = {
        "created": time.time(),
        "url": url,
        "name": final_name,
        "images": images
    }

    # Render results page
    items_html = ""
    for u in images[:500]:  # cap list rendering
        fn = os.path.basename(urlparse(u).path) or u
        items_html += f"<li><code>{fn}</code><br><a href='{u}' target='_blank' rel='noopener'>{u}</a></li>"

    count = len(images)
    note = ""
    if count == 0:
        note = "<p style='color:#b00020;'><b>No images found on that page.</b> If you expected photos, that site may load them dynamically or behind CSS/JS.</p>"

    return f"""
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Results</title>
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif; margin: 40px; max-width: 980px; }}
a {{ color: #0b57d0; }}
.card {{ background:#f6f6f7; border:1px solid #e6e6ea; border-radius:14px; padding:18px; }}
h1 {{ margin: 0 0 10px; }}
p {{ color:#444; }}
button {{ padding: 12px 16px; font-size: 16px; border-radius: 10px; border: 1px solid #111; background: #111; color: #fff; cursor: pointer; }}
code {{ background:#eee; padding:2px 6px; border-radius:6px; }}
ul {{ margin: 14px 0 0; padding-left: 18px; }}
li {{ margin: 10px 0; }}
.footer {{ margin-top: 18px; font-size: 12px; color: #777; }}
.toprow {{ display:flex; gap:12px; align-items:center; flex-wrap:wrap; }}
.badge {{ background:#111; color:#fff; padding:6px 10px; border-radius:999px; font-size:13px; }}
.small {{ font-size: 12px; color:#666; }}
</style>
</head>
<body>

<h1>Results</h1>
<div class="card">
  <div class="toprow">
    <div class="badge">{count} images found</div>
    <div class="small">ZIP name: <code>{final_name}.zip</code></div>
  </div>

  <p class="small">Page: <a href="{url}" target="_blank" rel="noopener">{url}</a></p>
  {note}

  <div style="margin-top:14px;">
    <a href="/download/{token}"><button {"disabled" if count == 0 else ""}>Download ZIP</button></a>
    <a href="/" style="margin-left:12px;">Back</a>
  </div>

  <ul>
    {items_html if count else ""}
  </ul>

  <div class="footer">Tool by RIMANO</div>
</div>

</body>
</html>
"""

@app.get("/download/{token}")
def download_zip(token: str):
    cleanup_old_results()
    data = RESULTS.get(token)
    if not data:
        raise HTTPException(status_code=404, detail="That download token has expired. Run the scan again.")

    s = build_session()
    tmpdir = tempfile.mkdtemp()
    zip_path = os.path.join(tmpdir, f"{data['name']}.zip")

    images = data["images"]
    if not images:
        raise HTTPException(status_code=400, detail="No images to download for that token.")

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for img_url in images:
            try:
                r = s.get(img_url, timeout=30, allow_redirects=True)
                r.raise_for_status()
                filename = os.path.basename(urlparse(img_url).path) or "image"
                file_path = os.path.join(tmpdir, filename)
                with open(file_path, "wb") as f:
                    f.write(r.content)
                z.write(file_path, filename)
            except Exception:
                continue

    return FileResponse(zip_path, filename=f"{data['name']}.zip")
