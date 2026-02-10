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
from fastapi.responses import HTMLResponse, FileResponse, RedirectResponse, Response

app = FastAPI()

IMG_RE = re.compile(r"\.(jpe?g|png|webp|gif|svg|avif)(\?.*)?$", re.IGNORECASE)

RESULTS = {}
TOKEN_TTL_SECONDS = 15 * 60


def build_session():
    s = requests.Session()
    retry = Retry(total=3, backoff_factor=0.8, status_forcelist=[429, 500, 502, 503, 504])
    adapter = HTTPAdapter(max_retries=retry)
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    s.headers.update({"User-Agent": "Mozilla/5.0"})
    return s


def cleanup_old_results():
    now = time.time()
    for t in list(RESULTS.keys()):
        if now - RESULTS[t]["created"] > TOKEN_TTL_SECONDS:
            RESULTS.pop(t, None)


def safe_zip_name(name: str) -> str:
    name = (name or "").strip()
    if not name:
        return "gallery"
    name = re.sub(r"[^a-zA-Z0-9._-]+", "-", name).strip("-")
    return name[:80] or "gallery"


def default_zip_name_from_url(url: str) -> str:
    p = urlparse(url)
    host = (p.netloc or "gallery").replace("www.", "")
    path = (p.path or "").strip("/")
    return host if not path else f"{host}-{path.split('/')[-1]}"


def extract_image_urls(page_url: str, html: str):
    soup = BeautifulSoup(html, "html.parser")
    found = []

    def add(u):
        if not u:
            return
        abs_u = urljoin(page_url, u.strip())
        if IMG_RE.search(abs_u):
            found.append(abs_u)

    for img in soup.select("img"):
        add(img.get("src", ""))

    for a in soup.select("a[href]"):
        add(a.get("href", ""))

    return list(dict.fromkeys(found))


ASSET_KEYWORDS = ["logo", "icon", "favicon", "badge", "facebook", "instagram", "visa", "mastercard"]

def looks_like_site_asset(url: str):
    u = url.lower()
    return any(k in u for k in ASSET_KEYWORDS)


def render_home(token="", thumb=0, error="", url_prefill="", name_prefill=""):
    cleanup_old_results()
    results_section = ""

    if token and token in RESULTS:
        data = RESULTS[token]
        images = data["images"]

        rows = ""
        for idx, img_url in enumerate(images):
            filename = os.path.basename(urlparse(img_url).path) or f"image-{idx+1}"

            thumb_html = ""
            if thumb:
                thumb_html = f"""
                <div class="thumb">
                    <img src="{img_url}" loading="lazy">
                </div>
                """

            rows += f"""
            <div class="item">
                <div class="chk">
                    <input class="pick" type="checkbox" name="idx" value="{idx}">
                </div>
                {thumb_html}
                <div class="main">
                    <div class="fn">{filename}</div>
                    <div class="actions">
                        <a class="btn" href="/view/{token}/{idx}?thumb={thumb}">View</a>
                        <a class="btn ghost" href="/one/{token}/{idx}">Download</a>
                    </div>
                </div>
            </div>
            """

        toggle = 0 if thumb else 1
        toggle_label = "Show thumbnails" if not thumb else "Hide thumbnails"

        results_section = f"""
        <div class="results">
            <div class="ctaRow">
                <a class="btn primary" href="/download/{token}">Download all images (ZIP)</a>
                <form method="post" action="/download-selected/{token}" id="selectedForm">
                    <button class="btn ghost" type="submit" id="btnSelected" disabled>
                        Download selected (ZIP)
                    </button>
                </form>
                <a class="btn ghost" href="/?t={token}&thumb={toggle}">{toggle_label}</a>
            </div>

            <div class="tools">
                <label><input type="checkbox" id="selectAll"> Select all</label>
                <span id="selCount">0 selected</span>
            </div>

            <form method="post" action="/download-selected/{token}" id="listForm">
                <div class="list">{rows}</div>
            </form>
        </div>

        <script>
        const picks = () => Array.from(document.querySelectorAll(".pick"));
        const selectAll = document.getElementById("selectAll");
        const btnSelected = document.getElementById("btnSelected");
        const selCount = document.getElementById("selCount");
        const listForm = document.getElementById("listForm");
        const selectedForm = document.getElementById("selectedForm");

        function update(){{
            const c = picks().filter(x=>x.checked).length;
            selCount.textContent = c + " selected";
            btnSelected.disabled = c===0;
        }}

        selectAll.addEventListener("change",()=>{{
            picks().forEach(x=>x.checked=selectAll.checked);
            update();
        }});

        document.addEventListener("change",e=>{{
            if(e.target.classList.contains("pick")) update();
        }});

        selectedForm.addEventListener("submit",(e)=>{{
            e.preventDefault();
            listForm.submit();
        }});
        </script>
        """

    err_html = f"<div class='error'>{error}</div>" if error else ""

    return f"""
<html>
<head>
<style>
body {{ font-family: system-ui; margin:40px; }}
.item {{ display:flex; gap:12px; padding:8px; border-top:1px solid #eee; }}
.chk input[type="checkbox"] {{ width:20px; height:20px; accent-color:#111; }}
.thumb img {{ width:60px; height:45px; object-fit:cover; }}
.actions {{ display:flex; gap:8px; }}
.btn {{ padding:8px 12px; border:1px solid #111; border-radius:10px; text-decoration:none; }}
.primary {{ background:#111; color:white; }}
.ghost {{ border-color:#ccc; }}
.list {{ border:1px solid #ddd; border-radius:10px; }}
.tools {{ margin:10px 0; }}
</style>
</head>
<body>

<h1>Gallery Grabber</h1>

<form method="post" action="/scan">
<input name="url" placeholder="https://example.com/page">
<input name="name" placeholder="ZIP name optional">
<label><input type="checkbox" name="hide_assets" checked> Hide logos/icons</label>
<button>Find images</button>
</form>

{err_html}
{results_section}

</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
def home(t: str = "", thumb: int = 0):
    return render_home(t, thumb)


@app.post("/scan")
def scan(url: str = Form(...), name: str = Form(""), hide_assets: str = Form("")):
    s = build_session()
    page = s.get(url)
    images = extract_image_urls(url, page.text)

    if hide_assets:
        images = [u for u in images if not looks_like_site_asset(u)]

    token = uuid.uuid4().hex
    RESULTS[token] = {
        "created": time.time(),
        "url": url,
        "name": safe_zip_name(name or default_zip_name_from_url(url)),
        "images": images,
    }

    return RedirectResponse(url=f"/?t={token}", status_code=303)


@app.get("/view/{token}/{idx}", response_class=HTMLResponse)
def view(token: str, idx: int, thumb: int = 0):
    img = RESULTS[token]["images"][idx]
    return f"""
    <a href="/?t={token}&thumb={thumb}">Back to results</a>
    <br><br>
    <img src="{img}" style="max-width:100%;">
    <br>
    <a href="/one/{token}/{idx}">Download</a>
    """


@app.get("/one/{token}/{idx}")
def one(token: str, idx: int):
    s = build_session()
    img_url = RESULTS[token]["images"][idx]
    r = s.get(img_url)
    return Response(r.content, media_type="application/octet-stream")
