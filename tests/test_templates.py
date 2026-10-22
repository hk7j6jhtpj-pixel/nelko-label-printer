import json

import pytest
from PIL import Image

from p21studio import templates
from p21studio.cli import main


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


def test_cli_uses_templates(tmp_path, capsys):
    f = tmp_path / "l.json"
    f.write_text(json.dumps({"elements": [{"type": "text", "x": 0, "y": 0, "w": 200, "h": 80, "text": "{{a}}"}]}), encoding="utf-8")
    assert main(["template", "save", "t1", str(f), "--json"]) == 0
    capsys.readouterr()
    assert main(["print", "-T", "t1", "--var", "a=X", "-d", f"mock:{tmp_path}", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["printed"] == 1


def test_image_path_with_variable_is_kept_absolute(tmp_path):
    icons = tmp_path / "icons"
    icons.mkdir()
    Image.new("L", (10, 10), 0).save(icons / "nut.png")
    f = tmp_path / "parts.p21.json"
    f.write_text(json.dumps({"elements": [{"type": "image", "x": 0, "y": 0, "w": 10, "h": 10, "src": "icons/{{icon}}.png"}]}), encoding="utf-8")
    from p21studio import label as labelmod, render

    templates.save_template("parts", labelmod.load(f))
    lab = labelmod.substitute(templates.load_template("parts"), {"icon": "nut"})
    assert not render.render(lab).errors


def test_variable_images_are_copied(tmp_path):
    from p21studio import label as labelmod

    icons = tmp_path / "icons"
    icons.mkdir()
    for n in ("nut", "washer"):
        Image.new("L", (10, 10), 0).save(icons / f"{n}.png")
    f = tmp_path / "parts.p21.json"
    f.write_text(json.dumps({"elements": [{"type": "image", "x": 0, "y": 0, "w": 10, "h": 10, "src": "icons/{{icon}}.png"}]}), encoding="utf-8")
    templates.save_template("parts2", labelmod.load(f))
    (icons / "nut.png").unlink()  # the template must not depend on the original folder
    templates.delete_template("parts2")
    assert not (templates.templates_dir() / "assets" / "parts2").exists()
