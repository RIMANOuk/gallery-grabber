import os
import re
import tempfile
import zipfile
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse, FileResponse

app = FastAPI()

IMG_RE = re.compile(r"\.(jpe?g|png|webp)(\?.*)?$", re.IGNORECASE)

HTML = """
<html>
<body>
<h2>Gallery Grabber</h2>
<form method="post" action="/download">
<input name="url" type="text" style="width:400px" placeholder="Paste gallery URL">
<button type="submit">Download</button>
</form>
</body>
</html>
"""

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

    return list(set(found))

@app.get("/", response_class=HTMLResponse)
def home():
    return HTML

@app.post("/download")
def download(url: str = Form(...)):
    tmpdir = tempfile.mkdtemp()
    zip_path = os.path.join(tmpdir, "gallery.zip")

    headers = {"User-Agent": "Mozilla/5.0"}
    page = requests.get(url, headers=headers)
    image_urls = extract_image_urls(url, page.text)

    with zipfile.ZipFile(zip_path, "w") as z:
        for img_url in image_urls:
            try:
                r = requests.get(img_url, headers=headers)
                filename = os.path.basename(urlparse(img_url).path)
                file_path = os.path.join(tmpdir, filename)

                with open(file_path, "wb") as f:
                    f.write(r.content)

                z.write(file_path, filename)
            except:
                pass

    return FileResponse(zip_path, filename="gallery.zip")
