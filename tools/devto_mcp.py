#!/usr/bin/env python3
"""dev.to MCP Server：把 Hugo markdown 文章同步到 dev.to，并管理 dev.to 文章。

依赖：mcp[cli], httpx
鉴权：环境变量 DEVTO_API_KEY（https://dev.to/settings/extensions 生成）
运行：python3 devto_mcp.py（stdio transport）
"""

import os
import re
import json
import shutil
import subprocess
import tempfile
import urllib.request
import urllib.error

import httpx
from mcp.server.fastmcp import FastMCP

API_KEY = os.environ.get("DEVTO_API_KEY", "")
BASE_URL = "https://dev.to/api"
BLOG_URL = "https://en.hancic.site"

# assets/diagrams/*.svg are inlined by the `diagram` shortcode so they can follow the
# light/dark theme through currentColor. dev.to can do neither: it renders no shortcode,
# and it proxies every external image through an imgproxy that hands the reader SVG bytes
# labelled "image/webp". So a PNG variant with the colour frozen and a white card behind
# it is published from static/, and the dev.to copy points at that URL.
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIAGRAM_SRC = os.path.join(REPO_ROOT, "assets", "diagrams")
DIAGRAM_OUT = os.path.join(REPO_ROOT, "static", "diagrams")
DIAGRAM_URL = f"{BLOG_URL}/diagrams"
INK = "#1e1e1e"
PAPER = "#ffffff"

# Rasteriser. The build host has no fonts and no cairo/rsvg/inkscape, so resvg ships as a
# pinned npm dependency (tools/raster) and the faces are fetched on first bake.
RASTER_DIR = os.path.join(REPO_ROOT, "tools", "raster")
RENDER_JS = os.path.join(RASTER_DIR, "render.mjs")
FONT_DIR = os.path.join(RASTER_DIR, "fonts")
FONT_VERSION = "0.4.2"
FONT_FILES = ("Inter_400Regular.ttf", "Inter_600SemiBold.ttf")
FONT_URL = ("https://cdn.jsdelivr.net/npm/@expo-google-fonts/inter@{v}/{w}/{n}.ttf")
RASTER_ZOOM = 2  # the blog shows them at 720 CSS px, so 2x matches a retina screen

mcp = FastMCP("devto")


class DevtoError(Exception):
    """dev.to API error."""


def _api(method: str, path: str, **kwargs) -> dict | list:
    if not API_KEY:
        raise DevtoError(
            "DEVTO_API_KEY is not set. Generate one at https://dev.to/settings/extensions "
            "and put it in the mcp.json entry for this server."
        )
    headers = {"Content-Type": "application/json", "api-key": API_KEY}
    headers.update(kwargs.pop("headers", {}))
    try:
        resp = httpx.request(method, f"{BASE_URL}{path}", headers=headers, timeout=60, **kwargs)
    except httpx.HTTPError as e:
        raise DevtoError(f"Request failed: {e}") from e
    if resp.status_code >= 400:
        try:
            err = resp.json().get("error", resp.text)
        except Exception:
            err = resp.text[:300]
        raise DevtoError(f"HTTP {resp.status_code}: {err}")
    if resp.status_code == 204:
        return {}
    return resp.json()


def _frontmatter_published(body_markdown: str, published: bool) -> str:
    """Rewrite the body's `published` field. dev.to gives it priority over the API field."""
    value = "true" if published else "false"
    if not body_markdown.startswith("---"):
        return body_markdown
    parts = body_markdown.split("---", 2)
    if len(parts) < 3:
        return body_markdown
    front, rest = parts[1], parts[2]
    if re.search(r"(?m)^\s*published:", front):
        front = re.sub(r"(?m)^(\s*)published:.*$", rf"\g<1>published: {value}", front)
    else:
        front = front.rstrip("\n") + f"\npublished: {value}\n"
    return f"---{front}---{rest}"


def _parse_frontmatter(content: str) -> tuple[dict, str]:
    """Split YAML frontmatter from body."""
    if not content.startswith("---"):
        return {}, content
    parts = content.split("---", 2)
    if len(parts) < 3:
        return {}, content
    yaml_block = parts[1].strip()
    body = parts[2].lstrip("\n")
    meta = {}
    for line in yaml_block.splitlines():
        line = line.strip()
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key == "tags" and val.startswith("["):
            tags = [t.strip().strip('"').strip("'") for t in val.strip("[]").split(",") if t.strip()]
            meta[key] = tags
        else:
            meta[key] = val
    return meta, body


def _convert_images(body: str, base_url: str) -> str:
    """Convert relative image paths to absolute URLs."""
    def md_img(match):
        alt, url = match.group(1), match.group(2)
        if url.startswith("http"):
            return match.group(0)
        if not url.startswith("/"):
            url = "/" + url
        return f"![{alt}]({base_url}{url})"
    body = re.sub(r'!\[([^\]]*)\]\(([^)]+)\)', md_img, body)

    def html_img(match):
        full, src = match.group(0), match.group(1)
        if src.startswith("http"):
            return full
        if not src.startswith("/"):
            src = "/" + src
        return full.replace(match.group(1), f"{base_url}{src}")
    body = re.sub(r'<img[^>]*src=["\']([^"\']+)["\'][^>]*>', html_img, body)
    return body


def _strip_shortcodes(body: str) -> str:
    """Convert Hugo shortcodes to plain markdown, remove the rest."""
    def figure(match):
        inner = match.group(1)
        src = re.search(r'src=["\']([^"\']+)["\']', inner)
        alt = re.search(r'(?:alt|title)=["\']([^"\']+)["\']', inner)
        if src:
            return f"![{alt.group(1) if alt else ''}]({src.group(1)})"
        return ""
    body = re.sub(r'\{\{<\s*figure\s+([^>]+)\s*>\}\}', figure, body)
    body = re.sub(r'\{\{<[^>]*>\}\}', '', body)
    body = re.sub(r'\{\{%[^%]*%\}\}', '', body)
    return body


def _sanitize_tags(tags: list[str]) -> list[str]:
    """dev.to rejects tags holding anything but lowercase letters and digits."""
    out = []
    for tag in tags:
        clean = re.sub(r"[^a-z0-9]", "", tag.lower())
        if clean and clean not in out:
            out.append(clean)
    return out[:4]


def _devto_name(source_name: str) -> str:
    """three-problems.svg -> three-problems.png (the name dev.to fetches)."""
    stem = source_name[:-4] if source_name.endswith(".svg") else source_name
    return f"{stem}.png"


def _convert_diagrams(body: str) -> str:
    """Expand the `diagram` shortcode into an image the blog serves."""
    def repl(match):
        src, caption = match.group(1), (match.group(2) or "").strip()
        image = f"![{caption}]({DIAGRAM_URL}/{_devto_name(src)})"
        return f"{image}\n\n*{caption}*" if caption else image
    return re.sub(r'\{\{<\s*diagram\s+"([^"]+)"\s*(?:"([^"]*)")?\s*>\}\}', repl, body)


def _bake_svg(svg: str) -> str:
    """Freeze currentColor to a fixed ink and lay a white card behind the drawing."""
    box = re.search(r'viewBox="[-\d.]+ [-\d.]+ ([\d.]+) ([\d.]+)"', svg)
    if not box:
        raise ValueError("viewBox missing")
    width, height = box.groups()
    card = f'<rect x="0" y="0" width="{width}" height="{height}" fill="{PAPER}"/>'
    svg = re.sub(r'(<svg\b[^>]*>)', lambda m: f"{m.group(1)}\n  {card}", svg, count=1)
    return svg.replace("currentColor", INK)


def _ensure_fonts() -> None:
    """Fetch the two Inter faces on first use. resvg draws no text without them."""
    os.makedirs(FONT_DIR, exist_ok=True)
    for name in FONT_FILES:
        path = os.path.join(FONT_DIR, name)
        if os.path.exists(path) and open(path, "rb").read(4) == b"\x00\x01\x00\x00":
            continue
        weight = "400Regular" if "400" in name else "600SemiBold"
        url = FONT_URL.format(v=FONT_VERSION, w=weight, n=name[:-4])
        try:
            with urllib.request.urlopen(url, timeout=60) as resp, open(path, "wb") as f:
                shutil.copyfileobj(resp, f)
        except (urllib.error.URLError, OSError) as exc:
            raise RuntimeError(f"cannot fetch {name} from {url}: {exc}") from exc
        if open(path, "rb").read(4) != b"\x00\x01\x00\x00":
            os.remove(path)
            raise RuntimeError(f"{url} did not return a TrueType font")


def _rasterise(svg: str, out_path: str) -> tuple[int, int]:
    """Render baked SVG to PNG and return its pixel size."""
    if shutil.which("node") is None:
        raise RuntimeError("node not found; install it to bake diagrams")
    if not os.path.isdir(os.path.join(RASTER_DIR, "node_modules", "@resvg")):
        raise RuntimeError(f"run `npm install` in {RASTER_DIR} first")
    _ensure_fonts()
    with tempfile.TemporaryDirectory() as tmp:
        source = os.path.join(tmp, "in.svg")
        with open(source, "w", encoding="utf-8") as f:
            f.write(svg)
        proc = subprocess.run(
            ["node", RENDER_JS, source, out_path, str(RASTER_ZOOM)],
            cwd=RASTER_DIR, capture_output=True, text=True, timeout=120,
        )
    if proc.returncode != 0:
        raise RuntimeError(f"resvg failed: {proc.stderr.strip()[:300]}")
    return _png_size(out_path)


def _png_size(path: str) -> tuple[int, int]:
    """Width and height from the PNG IHDR chunk."""
    with open(path, "rb") as f:
        head = f.read(24)
    if head[:8] != b"\x89PNG\r\n\x1a\n":
        raise RuntimeError(f"{path} is not a PNG")
    return int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big")


def _diagram_warnings(names: list[str]) -> list[str]:
    """Complain about diagrams the post references: not served yet, or baked stale."""
    warnings = []
    for name in names:
        url = f"{DIAGRAM_URL}/{name}"
        try:
            resp = httpx.head(url, timeout=10, follow_redirects=True)
            if resp.status_code >= 400:
                warnings.append(f"{url} -> HTTP {resp.status_code}")
        except httpx.HTTPError as exc:
            warnings.append(f"{url} -> unreachable ({exc.__class__.__name__})")
        stale = _stale_diagram(name)
        if stale:
            warnings.append(f"{url} -> {stale}")
    return warnings


def _stale_diagram(name: str) -> str:
    """Why the published copy of a diagram no longer matches assets/, if it does not."""
    source = os.path.join(DIAGRAM_SRC, name[:-4] + ".svg")
    if not os.path.exists(source):
        return ""
    target = os.path.join(DIAGRAM_OUT, name)
    if not os.path.exists(target):
        return "not baked yet, run bake_diagrams"
    if os.path.getmtime(source) > os.path.getmtime(target):
        return "static copy is older than assets/, re-run bake_diagrams"
    return ""


# ---------------- Tools ----------------

@mcp.tool(description=(
    "Bake a dev.to-friendly PNG of every assets/diagrams/*.svg into static/diagrams/, with "
    "currentColor frozen to a fixed ink on a white card. Needed because dev.to neither renders "
    "the shortcode nor inherits the blog's theming, and because its image proxy cannot rasterise "
    "SVG. Run this after adding or editing a diagram, then deploy the site so the URLs exist. "
    "Orphans left by a renamed diagram are removed; the first run installs nothing but does "
    "fetch the bundled Inter faces."
))
def bake_diagrams() -> dict:
    os.makedirs(DIAGRAM_OUT, exist_ok=True)
    baked, wanted = [], set()
    for name in sorted(os.listdir(DIAGRAM_SRC)):
        if not name.endswith(".svg"):
            continue
        target_name = _devto_name(name)
        wanted.add(target_name)
        with open(os.path.join(DIAGRAM_SRC, name), encoding="utf-8") as f:
            out = _bake_svg(f.read())
        target = os.path.join(DIAGRAM_OUT, target_name)
        width, height = _rasterise(out, target)
        baked.append({
            "file": os.path.relpath(target, REPO_ROOT),
            "url": f"{DIAGRAM_URL}/{target_name}",
            "pixels": f"{width}x{height}",
        })
    removed = []
    for stale in sorted(os.listdir(DIAGRAM_OUT)):
        if stale.endswith(".svg") or stale not in wanted:
            os.remove(os.path.join(DIAGRAM_OUT, stale))
            removed.append(stale)
    return {"baked": len(baked), "diagrams": baked, "removed": removed}

@mcp.tool(description=(
    "Sync a Hugo markdown post to dev.to. Converts relative image paths to absolute URLs "
    "(https://en.hancic.site/...), turns the diagram shortcode into images hosted on the blog, "
    "strips the remaining Hugo shortcodes, adds canonical_url pointing to your blog. "
    "Default: creates a draft. Set publish=true to publish immediately. "
    "Set dry_run=true to preview the converted markdown without posting. "
    "Either way it warns about diagram URLs the blog does not serve yet."
))
def sync_post(
    file_path: str,
    publish: bool = False,
    dry_run: bool = False,
) -> dict:
    with open(file_path, "r", encoding="utf-8") as f:
        content = f.read()

    meta, body = _parse_frontmatter(content)
    body = _convert_diagrams(body)
    body = _strip_shortcodes(body)
    body = _convert_images(body, BLOG_URL)

    diagrams = sorted({os.path.basename(u) for u in re.findall(
        rf'!\[[^\]]*\]\(({re.escape(DIAGRAM_URL)}/[^)\s]+)\)', body)})
    warnings = _diagram_warnings(diagrams)

    slug = meta.get("slug", "")
    canonical = f"{BLOG_URL}/{slug}/" if slug else BLOG_URL

    title = meta.get("title", "Untitled")
    tags = meta.get("tags", [])
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",") if t.strip()]
    tags = _sanitize_tags(tags)

    devto_body = f"---\ntitle: {title}\npublished: {'true' if publish else 'false'}\n"
    if tags:
        devto_body += f"tags: {', '.join(tags)}\n"
    devto_body += f"canonical_url: {canonical}\n---\n\n{body}"

    if dry_run:
        return {
            "dry_run": True,
            "title": title,
            "tags": tags,
            "canonical_url": canonical,
            "publish": publish,
            "diagrams": diagrams,
            "diagram_warnings": warnings,
            "body_markdown": devto_body,
        }

    result = _api("POST", "/articles", json={
        "article": {
            "title": title,
            "body_markdown": devto_body,
            "published": publish,
        }
    })
    return {
        "success": True,
        "url": result.get("url", ""),
        "id": result.get("id"),
        "published": publish,
        "canonical_url": canonical,
        "tags": tags,
        "diagrams": diagrams,
        "diagram_warnings": warnings,
    }


@mcp.tool(description="List your dev.to articles. Default: published. Set published=false for drafts. Use per_page (max 1000) and page to paginate.")
def list_articles(page: int = 1, per_page: int = 30, published: bool = True) -> list:
    params = {"page": page, "per_page": min(per_page, 1000)}
    # dev.to exposes drafts on a separate endpoint; the `published` query param is not honoured.
    path = "/articles/me/published" if published else "/articles/me/unpublished"
    return _api("GET", path, params=params)


@mcp.tool(description=(
    "Get a single dev.to article by its numeric ID, with full body_markdown. "
    "Published articles only: drafts are not served by this endpoint, list them instead."
))
def get_article(article_id: int) -> dict:
    return _api("GET", f"/articles/{article_id}")


@mcp.tool(description=(
    "Update an existing dev.to article. Pass the article ID and new body_markdown. "
    "Other fields (title, tags, canonical_url, published state) stay unchanged unless you pass them. "
    "When publish is passed, the body's front matter is rewritten to agree with it: dev.to lets the "
    "front matter's own `published` win over the API field, so a body carrying `published: false` "
    "would otherwise silently keep the article a draft."
))
def update_article(
    article_id: int,
    body_markdown: str,
    title: str = "",
    canonical_url: str = "",
    publish: bool | None = None,
) -> dict:
    article: dict = {"body_markdown": body_markdown}
    if title:
        article["title"] = title
    if canonical_url:
        article["canonical_url"] = canonical_url
    if publish is not None:
        article["published"] = publish
        article["body_markdown"] = _frontmatter_published(body_markdown, publish)
    result = _api("PUT", f"/articles/{article_id}", json={"article": article})
    return {"success": True, "url": result.get("url", ""), "id": result.get("id")}


if __name__ == "__main__":
    mcp.run()
