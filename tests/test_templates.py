import json

import pytest
from PIL import Image

from p21studio import templates
from p21studio.cli import main
from p21studio.mcp import Server


@pytest.fixture(autouse=True)
def tpl_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("P21_TEMPLATES_DIR", str(tmp_path / "tpl"))
    return tmp_path / "tpl"


def test_save_load_list_delete(tmp_path):
    lab = {"elements": [{"type": "text", "x": 0, "y": 0, "w": 100, "h": 40, "text": "{{品名}}"}]}
    templates.save_template("作り置き", lab)
    assert [t["name"] for t in templates.list_templates()] == ["作り置き"]
    assert templates.list_templates()[0]["variables"] == ["品名"]
    with pytest.raises(templates.TemplateError):
        templates.save_template("作り置き", lab)
    templates.save_template("作り置き", lab, overwrite=True)
    assert templates.load_template("作り置き")["elements"][0]["text"] == "{{品名}}"
    templates.delete_template("作り置き")
    assert templates.list_templates() == []


@pytest.mark.parametrize("bad", ["", "a/b", "..", "x:y", ".hidden", "a" * 61, "..\evil"])
def test_bad_names_rejected(bad):
    with pytest.raises(templates.TemplateError):
        templates.save_template(bad, {"elements": []})


def test_images_are_embedded(tmp_path):
    img = tmp_path / "logo.png"
    Image.new("L", (10, 10), 0).save(img)
    templates.save_template("logo", {"elements": [{"type": "image", "x": 0, "y": 0, "w": 10, "h": 10, "src": str(img)}]})
    img.unlink()
    assert templates.load_template("logo")["elements"][0]["src"].startswith("data:image/png;base64,")


def test_cli_and_mcp_use_templates(tmp_path, capsys):
    f = tmp_path / "l.json"
    f.write_text(json.dumps({"elements": [{"type": "text", "x": 0, "y": 0, "w": 200, "h": 80, "text": "{{a}}"}]}), encoding="utf-8")
    assert main(["template", "save", "t1", str(f), "--json"]) == 0
    capsys.readouterr()
    assert main(["print", "-T", "t1", "--var", "a=X", "-d", f"mock:{tmp_path}", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["printed"] == 1
    s = Server("mock:")
    r = s.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "p21_list_templates", "arguments": {}}})
    assert "t1" in r["result"]["content"][0]["text"]
    r = s.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "p21_render", "arguments": {"template": "t1", "variables": {"a": "Y"}}}})
    assert r["result"]["content"][0]["type"] == "image"
    r = s.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "p21_render", "arguments": {"template": "t1", "text": "x"}}})
    assert r["error"]["code"] == -32602
