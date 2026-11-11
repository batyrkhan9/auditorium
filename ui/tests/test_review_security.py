"""Regression coverage for the viewer review's browser and transport fixes."""

import base64
import json
import re
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace

import pytest
from auditorium_ui.app import Viewer, create_app
from auditorium_ui.demo import build_demo
from starlette.testclient import TestClient

TEMPLATES = Path(__file__).resolve().parents[1] / "src" / "auditorium_ui" / "templates"


class Stylesheets(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "link" and attrs.get("rel") == "stylesheet":
            self.links.append(attrs)


@pytest.mark.parametrize("template", ["base.html", "error.html"])
def test_external_stylesheets_have_integrity_and_cors(template):
    parser = Stylesheets()
    parser.feed((TEMPLATES / template).read_text())
    assert parser.links
    for link in parser.links:
        assert link["href"].startswith("https://cdn.jsdelivr.net/npm/@fontsource/")
        assert re.search(r"@\d+\.\d+\.\d+/", link["href"])
        algorithm, digest = link["integrity"].split("-", 1)
        assert algorithm == "sha384"
        assert len(base64.b64decode(digest, validate=True)) == 48
        assert link["crossorigin"] == "anonymous"


def test_result_shortcuts_ignore_quotes_and_backslashes_and_keep_navigation():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is needed to execute the browser shortcut handler")
    (script,) = re.findall(r"<script>(.*?)</script>", (TEMPLATES / "result.html").read_text(), re.S)
    harness = r"""
const assert = require("node:assert/strict");
let handler;
const selectors = [];
global.document = {
  activeElement: {tagName: "BODY"},
  addEventListener: (name, callback) => { assert.equal(name, "keydown"); handler = callback; },
  querySelector: (selector) => {
    selectors.push(selector);
    assert.ok(['a[data-key="j"]', 'a[data-key="k"]'].includes(selector));
    return {href: selector.includes('"j"') ? "/next" : "/previous"};
  },
};
global.location = {href: "/current"};
eval(SCRIPT);
for (const key of ['"', '\\', "x"]) {
  handler({key});
  assert.equal(location.href, "/current");
}
assert.equal(selectors.length, 0);
handler({key: "j"});
assert.equal(location.href, "/next");
handler({key: "k"});
assert.equal(location.href, "/previous");
for (const modifier of ["metaKey", "ctrlKey", "altKey"]) {
  handler({key: "j", [modifier]: true});
  assert.equal(location.href, "/previous");
}
for (const tagName of ["INPUT", "TEXTAREA", "SELECT"]) {
  document.activeElement.tagName = tagName;
  handler({key: "j"});
  assert.equal(location.href, "/previous");
}
assert.equal(selectors.length, 2);
"""
    subprocess.run([node, "-e", harness.replace("SCRIPT", json.dumps(script))], check=True)


def test_large_original_download_is_not_compressed(tmp_path, monkeypatch):
    dataset, preflight, _ = build_demo(tmp_path / "demo")
    original = tmp_path / "original.txt"
    content = b"original evidence document\n" * 1000
    original.write_bytes(content)
    monkeypatch.setattr(
        Viewer,
        "_document",
        lambda self, request: (SimpleNamespace(media_type="text/plain"), original),
    )
    with TestClient(create_app(preflight, dataset, demo=True)) as client:
        response = client.get("/document/file?id=large", headers={"Accept-Encoding": "gzip"})
    assert response.status_code == 200
    assert len(content) > 2048  # exercise the old gzip middleware's size threshold
    assert "content-encoding" not in response.headers
    assert int(response.headers["content-length"]) == len(content)
    assert response.content == content


@pytest.mark.parametrize("query", ["q=", "%71=search", "error=", "%65rror=message"])
def test_reflection_compression_guard_handles_blank_and_encoded_keys(tmp_path, query):
    dataset, preflight, _ = build_demo(tmp_path)
    with TestClient(create_app(preflight, dataset, demo=True)) as client:
        response = client.get("/?" + query, headers={"Accept-Encoding": "gzip"})
    assert response.status_code == 200
    assert 'name="csrf"' in response.text
    assert "content-encoding" not in response.headers
