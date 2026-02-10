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
from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse, FileResponse, RedirectResponse, Response

app = FastAPI()

# Image formats to catch
IMG_RE = re.compile(r"\.(jpe?g|png|webp|gif|svg|avif)(\?.*)?$", re.IGNORECASE)

# Token store (good enough for personal tool; resets if Render restarts)
RESULTS = {}
TOKEN_TTL_SECONDS = 15 * 60  # 15 minutes


def build_session():
    s = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=0.8,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "HEAD"],
    )
    adapter = HTTPAdapter(max_retries=retry)
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    s.headers.update(
        {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-GB,en;q=0.9",
            "Connection": "keep-alive",
        }
    )
    return s


def cleanup_old_results():
    now = time.time()
    expired = [t for t, v in RESULTS.items() if now - v["created"] > TOKEN_TTL_SECONDS]
    for t in expired:
        RESULTS.pop(t, None)


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

        # srcset: pick the biggest candidate
        srcset = img.get("srcset", "") or ""
        if srcset:
            best_url = None
            best_score = -1
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
                        try:
                            score = int(token[:-1])
                        except Exception:
                            score = 0
                    elif token.endswith("x"):
                        try:
                            score = int(float(token[:-1]) * 1000)
                        except Exception:
                            score = 0
                if score > best_score:
                    best_score = score
                    best_url = url
            if best_url:
                add(best_url)

        for attr in ["data-src", "data-lazy-src", "data-original", "data-image", "data-url"]:
            add(img.get(attr, ""))

        for attr in ["data-srcset", "data-lazy-srcset"]:
            val = img.get(attr, "") or ""
            if val:
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
                        try:
                            w = int(bits[1][:-1])
                        except Exception:
                            w = 0
                    if w > best_w:
                        best_w = w
                        best_url = url
                if best_url:
                    add(best_url)

    # OG/Twitter preview images
    for meta in soup.select('meta[property="og:image"], meta[name="twitter:image"]'):
        add(meta.get("content", ""))

    # Icons (sometimes useful, but filtered out if "Hide assets" is enabled)
    for link in soup.select('link[rel~="icon"], link[rel="apple-touch-icon"], link[rel="apple-touch-icon-precomposed"]'):
        add(link.get("href", ""))

    # Inline CSS background images
    for el in soup.select("[style]"):
        style = el.get("style", "") or ""
        for m in re.finditer(r"url\(([^)]+)\)", style, re.IGNORECASE):
            raw = m.group(1).strip().strip('"').strip("'")
            add(raw)

    # Dedup keep order
    return list(dict.fromkeys(found))


ASSET_KEYWORDS = [
    "logo", "logos", "favicon", "icon", "icons", "sprite", "badge",
    "apple-touch-icon", "android-chrome", "mstile", "site.webmanifest",
    "visa", "mastercard", "amex", "paypal", "klarna", "stripe",
    "facebook", "instagram", "linkedin", "twitter", "tiktok", "youtube", "pinterest",
    "cookie", "cookies", "gdpr", "consent",
]

def looks_like_site_asset(image_url: str) -> bool:
    u = image_url.lower()
    path = urlparse(u).path or ""
    filename = os.path.basename(path)

    # common favicon/icon paths
    if "favicon" in u or "/icons/" in u or "/icon/" in u:
        return True
    if filename in ["favicon.ico", "favicon.png", "favicon.svg"]:
        return True

    # keyword based
    for k in ASSET_KEYWORDS:
        if k in u:
            return True

    return False


def render_home(token: str = "", thumb: int = 0, error: str = "", url_prefill: str = "", name_prefill: str = "") -> str:
    cleanup_old_results()

    results_section = ""
    if token:
        data = RESULTS.get(token)
        if not data:
            error = error or "That session has expired. Run the scan again."
        else:
            images = data["images"]
            count = len(images)
            page_url = data["url"]
            zip_name = data["name"] + ".zip"
            hide_assets = data.get("hide_assets", False)

            toggle_thumb = 0 if thumb else 1
            toggle_label = "Show thumbnails" if not thumb else "Hide thumbnails"

            rows = ""
            for idx, img_url in enumerate(images[:500]):
                filename = os.path.basename(urlparse(img_url).path) or f"image-{idx+1}"

                thumb_html = ""
                if thumb:
                    thumb_html = f"""
                      <div class="thumb">
                        <img src="{img_url}" alt="{filename}" loading="lazy">
                      </div>
                    """

                rows += f"""
                  <div class="item">
                    {thumb_html}
                    <div class="main">
                      <div class="fn">{filename}</div>
                      <div class="actions">
                        <a class="btn" target="_blank" rel="noopener" href="/view/{token}/{idx}?thumb={thumb}">View</a>
                        <a class="btn ghost" href="/one/{token}/{idx}">Download</a>
                      </div>
                    </div>
                  </div>
                """

            filter_note = "Hide site assets: On" if hide_assets else "Hide site assets: Off"

            results_section = f"""
            <div class="results">
              <div class="resultsHead">
                <div class="pill">{count} images found</div>
                <div class="meta">
                  Source page: <a href="{page_url}" target="_blank" rel="noopener">Open</a>
                  <span class="sep">•</span>
                  {filter_note}
                </div>
              </div>

              <div class="ctaRow">
                <a class="btn primary" href="/download/{token}">Download all images (ZIP)</a>
                <div class="right">
                  <a class="btn ghost" href="/?t={token}&thumb={toggle_thumb}">{toggle_label}</a>
                  <div class="zipnote">ZIP name: <code>{zip_name}</code></div>
                </div>
              </div>

              <div class="list">
                {rows if count else "<div class='empty'>No images found on that page.</div>"}
              </div>
            </div>
            """

    err_html = f"<div class='error'>{error}</div>" if error else ""

    return f"""
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Gallery Grabber</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif; margin: 40px; max-width: 980px; }}
  h1 {{ margin: 0 0 6px; font-size: 34px; letter-spacing: -0.02em; }}
  p {{ margin: 0 0 18px; color: #444; line-height: 1.45; font-size: 14px; }}
  .card {{ background: #f6f6f7; border: 1px solid #e6e6ea; border-radius: 16px; padding: 18px; }}
  label {{ display: block; font-weight: 600; margin: 10px 0 8px; font-size: 13px; }}
  input[type=text] {{ width: 100%; box-sizing: border-box; padding: 12px 14px; font-size: 15px; border-radius: 12px; border: 1px solid #cfcfd6; }}
  .small {{ font-size: 12px; color: #777; margin-top: 10px; line-height: 1.35; }}
  .row {{ display: flex; gap: 10px; margin-top: 14px; align-items: center; flex-wrap: wrap; }}
  button {{ padding: 12px 16px; font-size: 15px; border-radius: 12px; border: 1px solid #111; background: #111; color: #fff; cursor: pointer; }}
  button:disabled {{ opacity: .6; cursor: not-allowed; }}
  .spinner {{ width: 16px; height: 16px; border: 2px solid #ddd; border-top: 2px solid #111; border-radius: 50%; display: none; animation: spin .9s linear infinite; }}
  @keyframes spin {{ to {{ transform: rotate(360deg); }} }}
  .status {{ font-size: 13px; display:none; color: #333; }}
  .error {{ margin-top: 14px; padding: 12px 14px; border: 1px solid #f3c6c6; background: #fff5f5; color: #8a1f1f; border-radius: 12px; font-size: 13px; }}

  .checkrow {{ display:flex; gap:10px; align-items:center; margin-top: 12px; }}
  .checkrow input {{ transform: scale(1.1); }}
  .checkrow label {{ margin: 0; font-weight: 500; font-size: 13px; }}

  .results {{ margin-top: 18px; }}
  .resultsHead {{ display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }}
  .pill {{ background: #111; color: #fff; padding: 6px 10px; border-radius: 999px; font-size: 12px; }}
  .meta {{ font-size: 12px; color: #666; }}
  .meta a {{ color: #0b57d0; text-decoration: none; }}
  .sep {{ margin: 0 8px; }}

  .btn {{ display: inline-block; padding: 10px 14px; font-size: 14px; border-radius: 12px; border: 1px solid #111; background: #fff; color: #111; text-decoration: none; }}
  .btn:hover {{ opacity: .92; }}
  .primary {{ background: #111; color: #fff; }}
  .ghost {{ border-color: #cfcfd6; }}

  .ctaRow {{ display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; margin: 12px 0; }}
  .right {{ display:flex; gap: 10px; align-items:center; flex-wrap: wrap; }}
  .zipnote {{ font-size: 12px; color: #666; }}
  code {{ background: #eee; padding: 2px 6px; border-radius: 8px; }}

  .list {{ background: #fff; border: 1px solid #e6e6ea; border-radius: 16px; overflow: hidden; }}
  .item {{ display: flex; gap: 12px; padding: 10px 14px; border-top: 1px solid #f0f0f3; }}
  .item:first-child {{ border-top: 0; }}
  .thumb {{ width: 72px; height: 54px; border-radius: 10px; overflow: hidden; border: 1px solid #eee; flex-shrink: 0; background: #fafafa; display:flex; align-items:center; justify-content:center; }}
  .thumb img {{ width: 100%; height: 100%; object-fit: cover; display:block; }}
  .main {{ display:flex; align-items:center; justify-content: space-between; gap: 12px; width: 100%; }}
  .fn {{ font-size: 13px; color: #111; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 620px; }}
  .actions {{ display: flex; gap: 8px; flex-shrink: 0; }}
  .empty {{ padding: 14px; font-size: 13px; color: #666; }}

  .footer {{ margin-top: 18px; font-size: 12px; color: #777; }}
</style>
</head>
<body>

<h1>Gallery Grabber</h1>
<p>Paste a page URL. It finds image URLs on that page and lets you download everything as a ZIP, or grab single images.</p>

<div class="card">
  <form id="form" method="post" action="/scan">
    <label>Page URL</label>
    <input name="url" type="text" required placeholder="https://example.com/page" value="{url_prefill}">

    <label>ZIP name (optional)</label>
    <input name="name" type="text" placeholder="" value="{name_prefill}">
    <div class="small">
      Leave blank and it’ll auto-name from the URL. Your browser will save to Downloads unless you’ve enabled “Ask where to save each file”.
    </div>

    <div class="checkrow">
      <input id="hide_assets" name="hide_assets" type="checkbox" value="1" checked>
      <label for="hide_assets">Hide logos, favicons, social icons, payment badges</label>
    </div>

    <div class="row">
      <button id="btn" type="submit">Find images</button>
      <div class="spinner" id="spin"></div>
      <div class="status" id="status">Fetching and scanning…</div>
    </div>
    {err_html}
    <div class="footer">Tool by RIMANO</div>
  </form>
</div>

{results_section}

<script>
const form = document.getElementById("form");
const btn = document.getElementById("btn");
const spin = document.getElementById("spin");
const status = document.getElementById("status");
form.addEventListener("submit", () => {{
  btn.disabled = true;
  spin.style.display = "inline-block";
  status.style.display = "inline-block";
}});
</script>

</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
def home(t: str = "", thumb: int = 0):
    return render_home(token=t, thumb=thumb)


@app.post("/scan")
def scan(url: str = Form(...), name: str = Form(""), hide_assets: str = Form("")):
    cleanup_old_results()
    s = build_session()

    try:
        page = s.get(url, timeout=30, allow_redirects=True)
        page.raise_for_status()
    except Exception as e:
        html = render_home(error=f"Could not fetch page: {e}", url_prefill=url, name_prefill=name)
        return HTMLResponse(html, status_code=400)

    images = extract_image_urls(url, page.text)

    hide = bool(hide_assets)
    if hide and images:
        images = [u for u in images if not looks_like_site_asset(u)]

    final_name = (name or "").strip() or default_zip_name_from_url(url)
    final_name = safe_zip_name(final_name)

    token = uuid.uuid4().hex
    RESULTS[token] = {
        "created": time.time(),
        "url": url,
        "name": final_name,
        "images": images,
        "hide_assets": hide,
    }

    return RedirectResponse(url=f"/?t={token}&thumb=0", status_code=303)


@app.get("/download/{token}")
def download_all(token: str):
    cleanup_old_results()
    data = RESULTS.get(token)
    if not data:
        raise HTTPException(status_code=404, detail="That session has expired. Run the scan again.")

    s = build_session()
    tmpdir = tempfile.mkdtemp()
    zip_path = os.path.join(tmpdir, f"{data['name']}.zip")

    images = data["images"]
    if not images:
        raise HTTPException(status_code=400, detail="No images to download for that session.")

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


@app.get("/one/{token}/{idx}")
def download_one(token: str, idx: int):
    cleanup_old_results()
    data = RESULTS.get(token)
    if not data:
        raise HTTPException(status_code=404, detail="That session has expired. Run the scan again.")

    images = data["images"]
    if idx < 0 or idx >= len(images):
        raise HTTPException(status_code=404, detail="Image not found.")

    img_url = images[idx]
    s = build_session()
    r = s.get(img_url, timeout=30, allow_redirects=True)
    r.raise_for_status()

    filename = os.path.basename(urlparse(img_url).path) or "image"
    content_type = r.headers.get("Content-Type", "application/octet-stream")
    return Response(
        content=r.content,
        media_type=content_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/view/{token}/{idx}", response_class=HTMLResponse)
def view_one(token: str, idx: int, thumb: int = 0):
    cleanup_old_results()
    data = RESULTS.get(token)
    if not data:
        raise HTTPException(status_code=404, detail="That session has expired. Run the scan again.")

    images = data["images"]
    if idx < 0 or idx >= len(images):
        raise HTTPException(status_code=404, detail="Image not found.")

    img_url = images[idx]
    filename = os.path.basename(urlparse(img_url).path) or "image"
    return f"""
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{filename}</title>
<style>
body {{ font-family: -apple-system, system-ui, Segoe UI, Roboto, Arial; margin: 24px; max-width: 1100px; }}
.top {{ display:flex; align-items:center; justify-content: space-between; gap: 12px; flex-wrap: wrap; }}
h1 {{ font-size: 16px; margin: 0; }}
a.btn {{ display:inline-block; padding: 10px 14px; border-radius: 12px; border: 1px solid #111; text-decoration:none; color:#111; }}
a.primary {{ background:#111; color:#fff; }}
img {{ margin-top: 16px; max-width: 100%; height: auto; border-radius: 14px; border: 1px solid #eee; }}
.small {{ font-size: 12px; color:#666; }}
</style>
</head>
<body>
  <div class="top">
    <h1>{filename}</h1>
    <div>
      <a class="btn" href="/?t={token}&thumb={thumb}">Back to results</a>
      <a class="btn primary" href="/one/{token}/{idx}">Download this image</a>
    </div>
  </div>
  <div class="small"><a href="{img_url}" target="_blank" rel="noopener">Open original</a></div>
  <img src="{img_url}" alt="{filename}">
</body>
</html>
"""
