"""Build LexTreeRAG-architecture.pdf from the slide files in this folder.

    pip install playwright && playwright install chromium
    python architecture/build_pdf.py

Each slide is one 1920x1080 page. GitHub Actions runs this on every change to the
slides (.github/workflows/architecture-pdf.yml) and commits the PDF.
Set LOCAL_FONTS=1 to use the Inter font installed on the machine instead of Google Fonts.
"""

import json
import os
import pathlib
import re
import tempfile

from playwright.sync_api import sync_playwright

HERE = pathlib.Path(__file__).resolve().parent
OUT = HERE / "LexTreeRAG-architecture.pdf"

# Draws <x-connector> lines and arrow heads as SVG, the way the slide editor does.
CONNECTORS_JS = r"""
() => {
  const NS = 'http://www.w3.org/2000/svg';
  for (const c of [...document.querySelectorAll('x-connector')]) {
    const color = c.style.color || '#000';
    const w = parseFloat(c.style.borderWidth) || 2;
    const dashed = (c.style.borderStyle || '').includes('dashed');
    const head = c.getAttribute('head') || 'end';
    const svg = document.createElementNS(NS, 'svg');
    let pts;
    if (c.hasAttribute('x1')) {
      const [x1, y1, x2, y2] = ['x1', 'y1', 'x2', 'y2'].map(a => parseFloat(c.getAttribute(a)));
      const r = c.getAttribute('route') || 'straight';
      if (r === 'hv') pts = [[x1, y1], [x2, y1], [x2, y2]];
      else if (r === 'vh') pts = [[x1, y1], [x1, y2], [x2, y2]];
      else if (r === 'elbow') {
        if (Math.abs(x2 - x1) >= Math.abs(y2 - y1)) { const m = (x1 + x2) / 2; pts = [[x1, y1], [m, y1], [m, y2], [x2, y2]]; }
        else { const m = (y1 + y2) / 2; pts = [[x1, y1], [x1, m], [x2, m], [x2, y2]]; }
      } else pts = [[x1, y1], [x2, y2]];
      svg.setAttribute('style', 'position:absolute;left:0;top:0;overflow:visible');
      svg.setAttribute('width', '1'); svg.setAttribute('height', '1');
    } else {
      const width = parseFloat(c.style.width) || 64, h = 8 * w;
      pts = [[0, h / 2], [width, h / 2]];
      svg.setAttribute('width', width); svg.setAttribute('height', h);
      svg.setAttribute('style', 'display:block;flex:none;overflow:visible');
    }
    const L = 4 * w, HW = 1.6 * w;
    const trim = (a, b) => { const dx = b[0] - a[0], dy = b[1] - a[1], d = Math.hypot(dx, dy) || 1; return [b[0] - dx / d * L * 0.8, b[1] - dy / d * L * 0.8]; };
    const line = pts.map(p => p.slice());
    const heads = [];
    if (head === 'end' || head === 'both') { const n = line.length; heads.push([line[n - 2], pts[n - 1]]); line[n - 1] = trim(line[n - 2], pts[n - 1]); }
    if (head === 'both') { heads.push([line[1], pts[0]]); line[0] = trim(line[1], pts[0]); }
    const pl = document.createElementNS(NS, 'polyline');
    pl.setAttribute('points', line.map(p => p.join(',')).join(' '));
    pl.setAttribute('fill', 'none'); pl.setAttribute('stroke', color); pl.setAttribute('stroke-width', w);
    pl.setAttribute('stroke-linecap', 'round'); pl.setAttribute('stroke-linejoin', 'round');
    if (dashed) pl.setAttribute('stroke-dasharray', `${3 * w} ${2.5 * w}`);
    svg.appendChild(pl);
    for (const [from, tip] of heads) {
      const dx = tip[0] - from[0], dy = tip[1] - from[1], d = Math.hypot(dx, dy) || 1, ux = dx / d, uy = dy / d;
      const bx = tip[0] - ux * L, by = tip[1] - uy * L;
      const tri = document.createElementNS(NS, 'polygon');
      tri.setAttribute('points', `${tip[0]},${tip[1]} ${bx - uy * HW},${by + ux * HW} ${bx + uy * HW},${by - ux * HW}`);
      tri.setAttribute('fill', color);
      svg.appendChild(tri);
    }
    c.replaceWith(svg);
  }
}
"""

BASE_CSS = """
@page {size:1920px 1080px; margin:0}
html, body {margin:0; padding:0}
section {width:1920px; height:1080px; box-sizing:border-box; position:relative; overflow:hidden;
         break-after:page; display:flex; flex-direction:column}
section * {box-sizing:border-box; margin:0}
div {min-width:0}
div:not([style*="display"]) {display:flex; flex-direction:column}
h1 {font-size:96px; font-weight:600; line-height:1.1}
h2 {font-size:64px; font-weight:600; line-height:1.15}
h3 {font-size:44px; font-weight:600; line-height:1.2}
p {font-size:32px; line-height:1.4}
ul {padding-left:1.2em}
table {border-collapse:collapse; width:100%}
th, td {padding:0.35em 0.6em; text-align:left; vertical-align:top; border-bottom:1px solid rgba(20,33,61,.15)}
th {font-weight:600}
a {color:inherit}
"""

LOCAL_FONTS_CSS = """
@font-face {font-family:'IBM Plex Sans'; src:local('Inter'); font-weight:400}
@font-face {font-family:'IBM Plex Sans'; src:local('Inter SemiBold'); font-weight:600}
@font-face {font-family:'IBM Plex Sans'; src:local('Inter Bold'); font-weight:700}
@font-face {font-family:'Space Grotesk'; src:local('Inter Display SemiBold'); font-weight:600}
@font-face {font-family:'Space Grotesk'; src:local('Inter Display Bold'); font-weight:700}
"""


def page_html() -> str:
    deck = json.loads((HERE / "deck.json").read_text(encoding="utf-8"))
    icons = json.loads((HERE / "icons.json").read_text(encoding="utf-8"))
    slides = []
    for sid in deck["order"]:
        html = (HERE / "slides" / f"{sid}.html").read_text(encoding="utf-8")
        html = re.sub(r"<aside>.*?</aside>", "", html, flags=re.S)          # speaker notes stay out
        html = re.sub(r'<x-icon name="(\w+)" style="([^"]*)"></x-icon>',
                      lambda m: f'<span style="display:block;{m.group(2)}">{icons[m.group(1)]}</span>', html)
        slides.append(html)
    if os.getenv("LOCAL_FONTS") == "1":
        fonts = f"<style>{LOCAL_FONTS_CSS}</style>"
    else:
        fonts = "".join(f'<link rel="stylesheet" href="{f["href"]}">' for f in deck.get("faces", {}).values()
                        if f.get("href"))
    return (f"<!doctype html><html><head><meta charset='utf-8'><title>{deck['title']}</title>{fonts}"
            f"<style>{BASE_CSS}</style></head><body>{''.join(slides)}</body></html>")


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        src = pathlib.Path(tmp) / "deck.html"
        src.write_text(page_html(), encoding="utf-8")
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path=os.getenv("CHROMIUM_PATH") or None)
            page = browser.new_page(viewport={"width": 1920, "height": 1080})
            page.goto(src.as_uri(), wait_until="networkidle")
            page.evaluate(CONNECTORS_JS)
            page.evaluate("document.fonts.ready.then(() => true)")
            page.pdf(path=str(OUT), width="1920px", height="1080px", print_background=True,
                     prefer_css_page_size=True)
            browser.close()
    print(f"Wrote {OUT}")


if __name__ == "__main__":
    main()
