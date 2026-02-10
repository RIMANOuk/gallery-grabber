import os
import re
import tempfile
import zipfile
from urllib.parse import urljoin, urlparse

import requests
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
    p { margin: 0 0 18px; color: #444; line-height: 1.45; }
    .card { background: #f6f6f7; border: 1px solid #e6e6ea; border-radius: 14px; padding: 18px; }
    label { display: block; font-weight: 600; margin: 10px 0 8px; }
    input[type=text] { width: 100%; box-sizing: border-box; padding: 12px 14px; font-size: 16px; border-radius: 10px; border: 1px solid #cfcfd6; }
    .row { display: flex; gap: 10px; margin-top: 14px; align-items: center; }
    button { padding: 12px 16px; font-size: 16px; border-radius: 10px; border: 1px solid #111; background: #111; color: #fff; cursor: pointer; }
    button:disabled { opacity: .6; cursor: not-allowed; }
    .hint { font-size: 14px; color: #666; margin-top: 12px; line-height: 1.4; }
    .footer { margin-top: 16px; font-size: 12px; color: #777; }
    .spinner { width: 16px; height: 16px; border: 2px solid #ddd; border-top: 2px solid #111; border-radius: 50%; display: none; animation: spin .9s linear infinite; }
    @keyframes spin { to { transform: rotate(360deg); } }
    .status { font-size: 14px; color: #333; display:none; }
    code { background: #eee; padding: 2px 6px; border-radius: 6px; }
  </style>
</head>
<body>
  <h1>Gallery Grabber</h1>
  <p>Paste a gallery page URL and it’ll return a ZIP of the images found on that page.</p>

  <div class="card">
    <form id="form" method="post" action="/download">
      <label for="url">Gallery URL</label>
      <input id="url" name="url" type="text" placeholder="https://example.com/gallery" required>

      <label for="name">ZIP name (optional)</label>
      <input id="name" name="name" type="text" placeholder="big-debate-2026">

      <div class="row">
        <button id="btn" type="submit">Download ZIP</button>
        <div class="spinner" id="spin"></div>
        <div class="status" id="status">Working on it…</div>
      </div>

      <div class="hint">
        Free plan note: first request can be slow if the service has been asleep.
      </div>
      <div class="footer">
        If you get an empty ZIP, the page might load images dynamically or block automated requests.
      </div>
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

def extract_image_urls(page_url: str, html: str):
    soup = BeautifulSoup(html, "html.parser")
    found = []

    # Direct links to image files
    for a in soup.select("a[href]"):
        href = a.get("href", "")
        abs_url = urljoin(page_url, href)
        if IMG_RE.search(abs_url):
            found.append(abs_url)

    # <img src> tags
    for img in soup.select("img[src]"):
        src = img.get("src", "")
        abs_url = urljoin(page_url, src)
        if IMG_RE.search(abs_url):
            found.append(abs_url)

    # de-dupe but keep order
    return list(dict.fromkeys(found))

def safe_zip_name(name: str) -> str:
    name = (name or "").strip()
    if not name:
        return "gallery"
    # keep it tidy
    name = re.sub(r"[^a-zA-Z0-9._-]+", "-", name).strip("-")
    return name[:80] or "gallery"

@app.get("/", response_class=HTMLResponse)
def home():
    return PAGE

@app.get("/health")
def health():
    return {"ok": True}

@app.get("/download", response_class=HTMLResponse)
def download_get():
    return "<p>Use the form on the homepage to submit a gallery URL.</p>"

@app.post("/download")
def download(url: str = Form(...), name: str = Form("")):
    tmpdir = tempfile.mkdtemp()
    zip_path = os.path.join(tmpdir, "gallery.zip")

    headers = {"User-Agent": "Mozilla/5.0"}

    try:
        page = requests.get(url, headers=headers, timeout=30)
        page.raise_for_status()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not fetch that page URL: {e}")

    image_urls = extract_image_urls(url, page.text)

    if not image_urls:
        raise HTTPException(status_code=400, detail="No image links were found on that page.")

    try:
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as z:
            for img_url in image_urls:
                try:
                    r = requests.get(img_url, headers=headers, timeout=30)
                    r.raise_for_status()

                    filename = os.path.basename(urlparse(img_url).path) or "image"
                    file_path = os.path.join(tmpdir, filename)

                    with open(file_path, "wb") as f:
                        f.write(r.content)

                    z.write(file_path, filename)
                except Exception:
                    continue
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed creating zip: {e}")

    if not os.path.exists(zip_path):
        raise HTTPException(status_code=500, detail="Zip was not created.")

    download_name = safe_zip_name(name) + ".zip"
    return FileResponse(zip_path, filename=download_name)
