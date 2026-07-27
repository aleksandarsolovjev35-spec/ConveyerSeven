import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML_PATH = ROOT / "vision/ui/templates/index.html"


def load_html() -> str:
    return HTML_PATH.read_text(encoding="utf-8")


def _asset_paths(pattern: str):
    html = load_html()
    for relative in re.findall(pattern, html):
        yield ROOT / "vision/ui/static" / relative


def load_javascript() -> str:
    paths = list(_asset_paths(r'<script src="/static/(js/[^"?]+)(?:\?[^" ]*)?"></script>'))
    if not paths:
        raise AssertionError("No JavaScript modules linked from production HTML")
    return "\n".join(path.read_text(encoding="utf-8") for path in paths)


def load_css() -> str:
    paths = list(_asset_paths(r'<link rel="stylesheet" href="/static/(css/[^"?]+)(?:\?[^" ]*)?">'))
    if not paths:
        raise AssertionError("No CSS modules linked from production HTML")
    return "\n".join(path.read_text(encoding="utf-8") for path in paths)


def javascript_paths():
    return list(_asset_paths(r'<script src="/static/(js/[^"?]+)(?:\?[^" ]*)?"></script>'))


def css_paths():
    return list(_asset_paths(r'<link rel="stylesheet" href="/static/(css/[^"?]+)(?:\?[^" ]*)?">'))
