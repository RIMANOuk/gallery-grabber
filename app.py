import os
import re
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

IMG_RE = re.compile(r"\.(jpe?g|png|webp)(\?.*)?$", re.IGNORECASE)

PAGE = """
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Gallery Grabber</title>
<style>
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif; margin: 40px; max-width: 860px; }
h1 { margin: 0 0 6px; font-size: 32px; }
p { margin: 0 0 18px; color: #444; }
.card { background: #f6f6f7; border: 1px solid #e6e6ea; border-radius: 14px; padding: 18px; }
label { display: block; font-weight: 600; margin: 10px 0 8px; }
input[type=text] { width: 100%; padding: 12px; font-size: 16px; border-radius: 10px; border: 1px solid #ccc; }
.row { display: flex; gap: 10px; margin-top: 14px; align-items: center; }
button { padding: 12px 16px; font-size: 16px; border-radius: 10px; border: 1px solid #111; background: #111; color: #fff; cursor: pointer; }
.footer { margin-top: 18px; font-size: 12px; color: #777; }
.spinner { width: 16px; height: 16px; border: 2px solid #ddd; border-top: 2px solid #111; border-radius: 50%; display: none; animation: spin .9s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }
.status { font-size: 14px; display:none; }
</style>
</head>
<body>

<h1>Gallery Grabber</h1>
<p>Paste a gallery page URL and download the images as a ZIP.</p>

<div class="card">
<form id="form" method="post" action="/download">
<label>Gallery URL</label>
<input name="url" type="text" required placeholder="https://example.com/gallery">

<label>ZIP name (optional)</label>
<input name="name" type="text" placeholder="event-name">

<div class="row">
<button id="btn" type="submit">Download ZIP</button>
<div class="spinner" id="spin"></div>
<div class="status" id="status">Working…</div>
</div>
</form>
</div>

<div class="footer">
Tool by RIMANO
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
    session = requests.Session()

    retry = Retry(
        total=3,
        backoff_factor=0.8,
        status_forcelist=[429, 500, 502, 503, 504],
    )

    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)

    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-GB,en;q=0.9",
        "Connection": "keep-alive",
    })

    return session

def extract_image_urls(page_url, html):
    soup = BeautifulSoup(html, "html.parser")
    found = []

    for a in soup.select("a[href]"):
        href = a.get("href", "")
        abs_url = urljoin(page_url, href)
        if IMG_RE.search(abs_url):
            found.append(abs_url)

    for img in soup.select("img[src]"):
        src = img.get("src", "")
        abs_url = urljoin(page_url, src)
        if IMG_RE.search(abs_url):
            found.append(abs_url)

    return list(dict.fromkeys(found))

def safe_zip_name(name):
    if not name:
        return "gallery"
    name = re.sub(r"[^a-zA-Z0-9._-]+", "-", name).strip("-")
    return name[:80] or "gallery"

@app.get("/", response_class=HTMLResponse)
def home():
    return PAGE

@app.post("/download")
def download(url: str = Form(...), name: str = Form("")):
    session = build_session()

    tmpdir = tempfile.mkdtemp()
    zip_path = os.path.join(tmpdir, "gallery.zip")

    try:
        page = session.get(url, timeout=30, allow_redirects=True)
        page.raise_for_status()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not fetch page: {e}")

    image_urls = extract_image_urls(url, page.text)

    if not image_urls:
        raise HTTPException(status_code=400, detail="No images found.")

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for img_url in image_urls:
            try:
                r = session.get(img_url, timeout=30)
                r.raise_for_status()
                filename = os.path.basename(urlparse(img_url).path) or "image"
                file_path = os.path.join(tmpdir, filename)

                with open(file_path, "wb") as f:
                    f.write(r.content)

                z.write(file_path, filename)
            except Exception:
                continue

    download_name = safe_zip_name(name) + ".zip"
    return FileResponse(zip_path, filename=download_name)
