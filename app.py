import base64
import hashlib
import io
import json
import os
import re
import sqlite3
import urllib.error
import urllib.request
import uuid
import zipfile
from datetime import datetime, timezone
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = Path(os.environ.get("ARCHILDBOOK_DATA", ROOT / "data")).resolve()
ASSETS = DATA / "assets"
SAMPLE = json.loads((ROOT / "sample.json").read_text(encoding="utf-8"))
LIMIT = 14 * 1024 * 1024
PAGE_LAYOUTS = {"framed", "edge", "quiet"}
PNG = b"\x89PNG\r\n\x1a\n"


def database():
    return sqlite3.connect(DATA / "studio.sqlite3")


def new_book(source):
    book = json.loads(json.dumps(source))
    book.update(id=uuid.uuid4().hex, version=0)
    for page in book["pages"]:
        page.update(images=[], selected="", text_review=None, art_review=None)
    return book


def initialize():
    ASSETS.mkdir(parents=True, exist_ok=True)
    with database() as db:
        db.execute("CREATE TABLE IF NOT EXISTS books (id TEXT PRIMARY KEY, version INTEGER, body TEXT)")
        db.execute("CREATE TABLE IF NOT EXISTS revisions (id TEXT, version INTEGER, body TEXT, PRIMARY KEY (id, version))")
        if not db.execute("SELECT 1 FROM books LIMIT 1").fetchone():
            insert(db, new_book(SAMPLE))


def insert(db, book):
    record = (book["id"], book["version"], json.dumps(book, ensure_ascii=False))
    db.execute("INSERT INTO books VALUES (?, ?, ?)", record)
    db.execute("INSERT INTO revisions VALUES (?, ?, ?)", record)


def load(db, book_id):
    row = db.execute("SELECT body FROM books WHERE id = ?", (book_id,)).fetchone()
    if not row:
        raise ValueError("找不到这本书")
    return json.loads(row[0])


def asset_path(asset_id):
    if not re.fullmatch(r"[a-f0-9]{64}\.(png|jpg)", asset_id):
        raise ValueError("无效的素材编号")
    path = ASSETS / asset_id
    if not path.is_file():
        raise ValueError("找不到素材")
    return path


def save_asset(content):
    if content.startswith(PNG):
        suffix = ".png"
    elif content.startswith(b"\xff\xd8\xff"):
        suffix = ".jpg"
    else:
        raise ValueError("请使用 PNG 或 JPEG")
    name = hashlib.sha256(content).hexdigest() + suffix
    temp = ASSETS / uuid.uuid4().hex
    temp.write_bytes(content)
    temp.replace(ASSETS / name)
    return name


def generate(book, page):
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        raise ValueError("请在终端设置 OPENAI_API_KEY 并重启，或上传插画")
    model = os.environ.get("ARCHILDBOOK_IMAGE_MODEL", "gpt-image-2")
    cast = [book["characters"][index] for index in page["cast"]]
    references = [char for char in cast if char["ref"]]
    prompt = (
        "Illustrate one page of an original children's picture book, ages 5–7. "
        "Warm hand-painted editorial illustration, expressive gestures. No text, letters or logos. "
        "Preserve character identity, age, clothing and palette in the ordered references. "
        f"Market context: {book['market']}. Scene: {page['scene']}. "
        f"Arabic prose for context only, do not render it: {page['text']}. "
        "Cast: " + " | ".join(
            f"{char['name']}: {char['description']}" for char in cast
        )
        + ". References in attachment order: " + ", ".join(char["name"] for char in references)
    )
    boundary = uuid.uuid4().hex
    parts = []

    def part(name, content, filename=None, media=None):
        header = f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"'
        if filename:
            header += f'; filename="{filename}"\r\nContent-Type: {media}'
        parts.extend([header.encode() + b"\r\n\r\n", content, b"\r\n"])

    part("model", model.encode())
    part("prompt", prompt.encode())
    part("size", b"1024x1024")
    part("quality", b"medium")
    for char in references:
        path = asset_path(char["ref"])
        part("image[]", path.read_bytes(), path.name, "image/png" if path.suffix == ".png" else "image/jpeg")
    headers = {"Authorization": f"Bearer {key}"}
    if references:
        parts.append(f"--{boundary}--\r\n".encode())
        endpoint = "edits"
        body = b"".join(parts)
        headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
    else:
        endpoint = "generations"
        body = json.dumps({"model": model, "prompt": prompt, "size": "1024x1024", "quality": "medium"}).encode()
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(f"https://api.openai.com/v1/images/{endpoint}", body, headers)
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            result = json.load(response)
    except urllib.error.HTTPError as exc:
        raise ValueError(f"图像服务返回 HTTP {exc.code}，请检查账户、模型与额度") from exc
    except OSError as exc:
        raise ValueError("图像请求中断，请确认服务账单后再重试") from exc
    return {"id": save_asset(base64.b64decode(result["data"][0]["b64_json"])),
            "source": model, "prompt": prompt, "refs": [char["ref"] for char in references]}


def revise(book, action, data):
    if action == "metadata":
        if book["title"] != data["title"].strip() or book["market"] != data["market"].strip():
            book["cover_review"] = None
        if book["market"] != data["market"].strip():
            for page in book["pages"]:
                page["art_review"] = None
        book.update(title=data["title"].strip(), author=data["author"].strip(), market=data["market"].strip())
        if not book["title"]:
            raise ValueError("请输入书名")
        return
    if action == "cover":
        book["cover"] = save_asset(base64.b64decode(data["image"], validate=True))
        book["cover_review"] = None
        return
    if action == "review_cover":
        if not book.get("cover") or not data["reviewer"].strip():
            raise ValueError("请先上传封面并填写审核人")
        book["cover_review"] = data["reviewer"].strip()
        return
    if action == "character":
        index = data["index"]
        if index == len(book["characters"]):
            book["characters"].append({"name": "", "description": "", "ref": ""})
        char = book["characters"][index]
        if "image" in data:
            char["ref"] = save_asset(base64.b64decode(data["image"], validate=True))
        else:
            char.update(name=data["name"], description=data["description"])
        for page in book["pages"]:
            if index in page["cast"]:
                page["art_review"] = None
        return
    if action == "add_page":
        book["pages"].append(dict(text="", scene="", cast=[], images=[], selected="", text_review=None, art_review=None))
        return
    page = book["pages"][data["index"]]
    if action == "edit":
        if any(type(i) is not int or not 0 <= i < len(book["characters"]) for i in data["cast"]):
            raise ValueError("角色选择无效")
        layout = data.get("layout", page.get("layout", "framed"))
        if layout not in PAGE_LAYOUTS:
            raise ValueError("未知版式")
        if page["text"] != data["text"]:
            page["text_review"] = None
        if layout != page.get("layout", "framed") or any(
            page[field] != data[field] for field in ("text", "scene", "cast")
        ):
            page["art_review"] = None
        page.update(text=data["text"], scene=data["scene"], cast=data["cast"], layout=layout)
    elif action == "upload":
        page["images"].append({"id": save_asset(base64.b64decode(data["image"], validate=True)), "source": "上传"})
    elif action == "select":
        if data["asset"] not in [image["id"] for image in page["images"]]:
            raise ValueError("请先添加插画候选")
        if page["selected"] != data["asset"]:
            page.update(selected=data["asset"], art_review=None)
    elif action == "review":
        kind = data["kind"]
        if kind not in ("text", "art") or data["decision"] not in ("批准", "退回"):
            raise ValueError("无效的审核决定")
        if not data["reviewer"].strip() or (data["decision"] == "退回" and not data["note"].strip()):
            raise ValueError("请填写审核人，退回时请填写原因")
        if data["decision"] == "批准" and not (page["text"].strip() if kind == "text" else page["selected"]):
            raise ValueError("先填写正文或选择插画")
        page[kind + "_review"] = dict(reviewer=data["reviewer"], note=data["note"],
                                     decision=data["decision"], version=book["version"])
    else:
        raise ValueError("未知操作")


def epub(book):
    if not book["author"] or not book.get("cover_review") or not all(
        page["selected"] and all((page[field] or {}).get("decision") == "批准"
                                 for field in ("text_review", "art_review"))
        for page in book["pages"]
    ):
        raise ValueError("正式导出需要署名、封面审核及所有页面的正文、插画批准")
    result = io.BytesIO()
    title = escape(book["title"])
    manifest, spine, links = [], [], []
    with zipfile.ZipFile(result, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("mimetype", "application/epub+zip", zipfile.ZIP_STORED)
        archive.writestr("META-INF/container.xml", """<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
<rootfiles><rootfile full-path="OEBPS/book.opf" media-type="application/oebps-package+xml"/></rootfiles></container>""")
        manifest.append('<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>')
        archive.write(ROOT / "book.css", "OEBPS/book.css")
        manifest.append('<item id="style" href="book.css" media-type="text/css"/>')
        cover_path = asset_path(book["cover"])
        cover_name = "cover" + cover_path.suffix
        cover_media = "png" if cover_path.suffix == ".png" else "jpeg"
        archive.write(cover_path, f"OEBPS/{cover_name}")
        manifest.append(f'<item id="cover" href="{cover_name}" media-type="image/{cover_media}" properties="cover-image"/>')
        manifest.append('<item id="front" href="front.xhtml" media-type="application/xhtml+xml"/>')
        spine.append('<itemref idref="front"/>')
        archive.writestr("OEBPS/front.xhtml", f"""<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="ar" dir="rtl">
<head><title>{title}</title><meta name="viewport" content="width=900,height=1200"/><link rel="stylesheet" href="book.css"/><style>html,body{{margin:0}} .book-page{{width:900px;height:1200px}}</style></head>
<body><main class="book-page" data-layout="cover"><h1 class="book-title">{title}</h1>
<div class="book-art"><img src="{cover_name}" alt=""/></div>
<p class="book-byline">{escape(book["author"])}</p></main></body></html>""")
        for number, page in enumerate(book["pages"], 1):
            path = asset_path(page["selected"])
            name = f"art-{number}{path.suffix}"
            ext = "png" if path.suffix == ".png" else "jpeg"
            archive.write(path, f"OEBPS/{name}")
            manifest.append(f'<item id="art{number}" href="{name}" media-type="image/{ext}"/>')
            manifest.append(f'<item id="p{number}" href="p{number}.xhtml" media-type="application/xhtml+xml"/>')
            spine.append(f'<itemref idref="p{number}"/>')
            links.append(f'<li><a href="p{number}.xhtml">{number}</a></li>')
            paragraphs = "".join(f"<p>{escape(line)}</p>" for line in page["text"].splitlines() if line.strip())
            archive.writestr(f"OEBPS/p{number}.xhtml", f"""<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="ar" lang="ar" dir="rtl">
<head><title>{title}</title><meta name="viewport" content="width=900,height=1200"/>
<link rel="stylesheet" href="book.css"/><style>html,body{{margin:0}} .book-page{{width:900px;height:1200px}}</style></head>
<body><main class="book-page" data-layout="{page.get('layout', 'framed')}"><div class="book-art"><img src="{name}" alt=""/></div><div class="book-copy">{paragraphs}</div></main></body></html>""")
        archive.writestr("OEBPS/nav.xhtml", f"""<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" xml:lang="ar" dir="rtl">
<head><title>المحتويات</title></head><body><nav epub:type="toc"><h1>المحتويات</h1><ol>{''.join(links)}</ol></nav></body></html>""")
        modified = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        archive.writestr("OEBPS/book.opf", f"""<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="uid" xml:lang="ar" dir="rtl">
<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="uid">urn:uuid:{uuid.UUID(book['id'])}</dc:identifier>
<dc:title>{title}</dc:title><dc:language>ar</dc:language><dc:creator>{escape(book['author'])}</dc:creator>
<meta property="dcterms:modified">{modified}</meta><meta property="rendition:layout">pre-paginated</meta></metadata>
<manifest>{''.join(manifest)}</manifest><spine page-progression-direction="rtl">{''.join(spine)}</spine></package>""")
    return result.getvalue()


class Handler(BaseHTTPRequestHandler):
    def send(self, body, media, status=200, filename=None):
        self.send_response(status)
        self.send_header("Content-Type", media)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        self.wfile.write(body)

    def json(self, value, status=200):
        self.send(json.dumps(value, ensure_ascii=False).encode(), "application/json", status)

    def do_GET(self):
        parts = self.path.split("/")
        try:
            if self.path == "/":
                self.send((ROOT / "studio.html").read_bytes(), "text/html; charset=utf-8")
            elif self.path == "/book.css":
                self.send((ROOT / "book.css").read_bytes(), "text/css; charset=utf-8")
            elif parts[1] == "assets" and len(parts) == 3:
                path = asset_path(parts[2])
                self.send(path.read_bytes(), "image/png" if path.suffix == ".png" else "image/jpeg")
            elif self.path == "/api/books":
                with database() as db:
                    books = [json.loads(row[0]) for row in db.execute("SELECT body FROM books ORDER BY rowid")]
                self.json({"books": books, "ai": bool(os.environ.get("OPENAI_API_KEY"))})
            elif len(parts) == 5 and parts[1:3] == ["api", "books"] and parts[4] == "epub":
                with database() as db:
                    book = load(db, parts[3])
                self.send(epub(book), "application/epub+zip", filename="arabic-story.epub")
            else:
                self.json({"error": "找不到页面"}, 404)
        except ValueError as exc:
            self.json({"error": str(exc)}, 400)

    def do_POST(self):
        if self.headers.get("Host") not in (f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"):
            return self.json({"error": "请从本地访问"}, 403)
        if self.headers.get_content_type() != "application/json":
            return self.json({"error": "需要 JSON 请求"}, 415)
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= LIMIT:
                return self.json({"error": "文件过大或请求为空"}, 413)
            data = json.loads(self.rfile.read(size))
            with database() as db:
                if self.path == "/api/books":
                    title = data["title"].strip()
                    if not title:
                        raise ValueError("请输入书名")
                    book = new_book(dict(title=title, author="", market="沙特、阿联酋", characters=[],
                                         pages=[dict(text="", scene="", cast=[])]))
                    insert(db, book)
                elif self.path == "/api/edit":
                    book = load(db, data["id"])
                    if book["version"] != data["version"]:
                        return self.json({"error": "书稿已在其他窗口修改，请刷新后重试"}, 409)
                    if data["action"] == "generate":
                        page = book["pages"][data["index"]]
                        if not page["scene"].strip():
                            raise ValueError("请先保存本页分镜")
                        page["images"].append(generate(book, page))
                    else:
                        revise(book, data["action"], data)
                    book["version"] += 1
                    record = json.dumps(book, ensure_ascii=False)
                    updated = db.execute("UPDATE books SET version=?, body=? WHERE id=? AND version=?",
                                         (book["version"], record, book["id"], data["version"]))
                    if updated.rowcount != 1:
                        return self.json({"error": "书稿已改变，请刷新后重试"}, 409)
                    db.execute("INSERT INTO revisions VALUES (?, ?, ?)", (book["id"], book["version"], record))
                else:
                    return self.json({"error": "找不到操作"}, 404)
            self.json(book)
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            self.json({"error": str(exc)}, 400)


if __name__ == "__main__":
    initialize()
    port = int(os.environ.get("ARCHILDBOOK_PORT", 8765))
    with ThreadingHTTPServer(("127.0.0.1", port), Handler) as server:
        print(f"童书工作台 · http://127.0.0.1:{server.server_port}", flush=True)
        server.serve_forever()
