"""Regressions for issues found by the agent test round (2026-10-17)."""

import json

import pytest
from PIL import Image

from p21studio import api, label as labelmod
from p21studio.cli import main
from p21studio.printer import P21
from p21studio.render import render
from p21studio.transports import MockTransport, TransportError


# --- printing safety -----------------------------------------------------------
def test_missing_image_refuses_to_print(tmp_path):
    lab = {"elements": [{"type": "image", "x": 0, "y": 0, "w": 50, "h": 50, "src": str(tmp_path / "nope.png")}]}
    with pytest.raises(api.RenderErrors):
        api.print_labels(lab, f"mock:{tmp_path}")


def test_batch_failure_reports_what_was_printed(tmp_path, monkeypatch):
    calls = {"n": 0}
    orig = P21.print_image

    def flaky(self, *a, **kw):
        calls["n"] += 1
        if calls["n"] == 2:
            raise TransportError("link lost")
        return orig(self, *a, **kw)

    monkeypatch.setattr(P21, "print_image", flaky)
    lab = {"elements": [{"type": "text", "x": 0, "y": 0, "w": 200, "h": 80, "text": "{{a}}"}]}
    res = api.print_labels(lab, f"mock:{tmp_path}", rows=[{"a": "1"}, {"a": "2"}, {"a": "3"}])
    assert res["ok"] is False and res["printed"] == 1 and "link lost" in res["error"]


def test_portrait_mock_png_is_saved_upright(tmp_path):
    lab = {"orientation": "portrait", "elements": [{"type": "rect", "x": 0, "y": 0, "w": 10, "h": 10, "fill": True}]}
    res = api.print_labels(lab, f"mock:{tmp_path}")
    img = Image.open(res["results"][0]["mock_png"][0])
    assert img.size == (96, 284) and img.getpixel((0, 0)) == 0


# --- rendering / label model -----------------------------------------------------
def test_auto_size_is_not_fooled_by_kinsoku():
    text = "重要：このサーバーは本番環境です。再起動する前に必ず管理者に連絡してください"
    lab = {"elements": [{"type": "text", "x": 3, "y": 3, "w": 278, "h": 90, "text": text}]}
    auto = render(lab).boxes[0]["font_size"]
    for size in range(auto + 1, auto + 13):
        fixed = {"elements": [{**lab["elements"][0], "size": size}]}
        assert any("overflows" in w for w in render(fixed).warnings), f"{size}px fits but auto chose {auto}px"


def test_barcode_rewrite_and_quality_warnings():
    w = render({"elements": [{"type": "barcode", "x": 0, "y": 0, "w": 280, "h": 90, "data": "4901234567890", "symbology": "jan"}]}).warnings
    assert any("4901234567894" in x for x in w)
    w = render({"elements": [{"type": "barcode", "x": 0, "y": 0, "w": 100, "h": 90, "data": "4901234567894", "symbology": "ean13"}]}).warnings
    assert any("1px per module" in x for x in w)
    r = render({"elements": [{"type": "barcode", "x": 0, "y": 0, "w": 280, "h": 90, "data": "(01)12345678901231", "symbology": "gs1_128"}]})
    assert not r.errors


def test_warnings_for_typos_fonts_tofu_and_unresolved_vars():
    lab = {"elements": [{"type": "text", "x": 0, "y": 0, "w": 280, "h": 90, "text": "{{nobody}} 😀", "algin": "left", "font": "NoSuchFont"}]}
    w = "\n".join(render(labelmod.substitute(labelmod.normalize(lab), {})).warnings)
    assert "algin" in w and "NoSuchFont" in w and "😀" in w and "{{nobody}}" in w


def test_variable_names_with_long_vowel_mark():
    lab = labelmod.normalize({"elements": [{"type": "text", "x": 0, "y": 0, "w": 100, "h": 40, "text": "{{カテゴリー}}/{{佐々木}}"}]})
    assert labelmod.find_variables(lab) == ["カテゴリー", "佐々木"]
    assert labelmod.substitute(lab, {"カテゴリー": "A", "佐々木": "B"})["elements"][0]["text"] == "A/B"


@pytest.mark.parametrize("bad", [
    [1, 2],
    {"media": 5, "elements": []},
    {"variables": [1], "elements": []},
    {"elements": [{"type": "text", "x": float("nan"), "y": 0, "w": 10, "h": 10}]},
    {"media": {"length_mm": 0}, "elements": []},
    {"media": {"length_dots": "abc"}, "elements": []},
    {"copies": "x", "elements": []},
    {"elements": [{"type": "text", "x": 0, "y": 0, "w": 10, "h": 10, "line_spacing": -1}]},
    {"elements": [{"type": "text", "x": 0, "y": 0, "w": 10, "h": 10, "bold": "maybe"}]},
])
def test_bad_labels_raise_label_error(bad):
    with pytest.raises(labelmod.LabelError):
        labelmod.normalize(bad)


def test_string_booleans_and_none_text():
    lab = labelmod.normalize({"invert": "false", "elements": [{"type": "text", "x": 0, "y": 0, "w": 10, "h": 10, "text": None, "bold": "no"}]})
    assert lab["invert"] is False and lab["elements"][0]["bold"] is False and lab["elements"][0]["text"] == ""


def test_bad_date_format_is_a_label_error():
    lab = labelmod.normalize({"elements": [{"type": "text", "x": 0, "y": 0, "w": 10, "h": 10, "text": "{{date:%Q}}"}]})
    with pytest.raises(labelmod.LabelError):
        labelmod.substitute(lab)


def test_16bit_png_and_transparent_logo_inversion(tmp_path):
    p16 = tmp_path / "g16.png"
    Image.new("I;16", (40, 40), 30000).save(p16)
    r = render({"elements": [{"type": "image", "x": 0, "y": 0, "w": 40, "h": 40, "src": str(p16), "dither": "threshold", "threshold": 200}]})
    assert r.image.getbbox() is not None  # 30000/65535 grey is darker than 200/255
    logo = Image.new("RGBA", (40, 40), (0, 0, 0, 0))
    logo.paste((0, 0, 0, 255), (10, 10, 30, 30))
    lp = tmp_path / "logo.png"
    logo.save(lp)
    r = render({"elements": [{"type": "image", "x": 0, "y": 0, "w": 40, "h": 40, "src": str(lp), "dither": "threshold", "invert": True}]})
    assert r.image.getpixel((2, 2)) == 255  # transparent corner stays white when inverted


def test_csv_shift_jis_and_missing_cells(tmp_path):
    f = tmp_path / "sjis.csv"
    f.write_bytes("名前,品名\n佐藤,カレー\n鈴木\n".encode("cp932"))
    assert labelmod.read_csv(str(f)) == [{"名前": "佐藤", "品名": "カレー"}, {"名前": "鈴木", "品名": ""}]


# --- CLI ---------------------------------------------------------------------------
def test_render_never_overwrites_the_source_image(tmp_path, capsys):
    img = tmp_path / "photo.png"
    Image.new("L", (50, 50), 0).save(img)
    before = img.read_bytes()
    assert main(["render", str(img), "--json"]) == 0
    assert img.read_bytes() == before and (tmp_path / "photo.preview.png").exists()
    assert main(["render", str(img), "-o", str(img), "--json"]) == 5


def test_cli_bad_inputs_give_json_errors(tmp_path, capsys):
    f = tmp_path / "bad.json"
    f.write_text("[1,2]", encoding="utf-8")
    assert main(["render", str(f), "--json"]) == 5
    assert json.loads(capsys.readouterr().out)["ok"] is False
    assert main(["print", "-t", "x", "-d", "rfcomm:AA:BB:CC:DD:EE:FF@abc", "--json"]) == 4


# --- round 2 (re-verification) ---------------------------------------------------
def test_auto_size_when_minimum_size_overflows_by_kinsoku():
    text = "注意：この機器は高温になります。使用中および使用直後は触れないでください。"
    r = render({"elements": [{"type": "text", "x": 3, "y": 3, "w": 278, "h": 90, "text": text}]})
    assert r.boxes[0]["font_size"] >= 18 and not r.warnings


def test_ean_guard_bars_are_drawn():
    plain = render({"elements": [{"type": "barcode", "x": 0, "y": 0, "w": 280, "h": 90, "data": "5901234123457", "symbology": "ean13"}]})
    guard = render({"elements": [{"type": "barcode", "x": 0, "y": 0, "w": 280, "h": 90, "data": "5901234123457", "symbology": "ean13-guard"}]})
    assert not guard.warnings and not guard.errors
    ink = lambda r: r.image.histogram()[0]  # noqa: E731
    assert ink(guard) > ink(plain) * 0.9


def test_uniform_16bit_grey_is_not_blank(tmp_path):
    p = tmp_path / "flat16.png"
    Image.new("I;16", (40, 40), 8000).save(p)  # dark grey
    r = render({"elements": [{"type": "image", "x": 0, "y": 0, "w": 40, "h": 40, "src": str(p), "dither": "threshold"}]})
    assert r.image.getpixel((20, 20)) == 0


def test_length_dots_rounds_like_the_gui():
    from p21studio import protocol

    assert protocol.length_dots(75) == 533


def test_render_exit_code_when_elements_fail(tmp_path, capsys):
    f = tmp_path / "l.json"
    f.write_text(json.dumps({"elements": [{"type": "image", "x": 0, "y": 0, "w": 9, "h": 9, "src": "missing.png"}]}), encoding="utf-8")
    assert main(["render", str(f), "-o", str(tmp_path / "o.png"), "--json"]) == 5


# --- round 3: roll running out mid-batch (seen on hardware 2026-10-18) -------------
def test_running_out_of_labels_is_reported(tmp_path, monkeypatch):
    from p21studio import transports

    made = []

    def mock_with_short_roll(spec):
        t = MockTransport(str(tmp_path), labels_left=2)
        made.append(t)
        return t

    monkeypatch.setattr("p21studio.printer.from_spec", mock_with_short_roll)
    lab = {"elements": [{"type": "text", "x": 0, "y": 0, "w": 200, "h": 80, "text": "{{a}}"}]}
    res = api.print_labels(lab, "mock:", rows=[{"a": str(i)} for i in range(4)])
    assert res["ok"] is False
    assert res["printed"] == 2 and res["uncertain"] == [3] and res["not_sent"] == 1
    assert any("Out of labels" in w for w in res["results"][2]["warnings"])
    assert len(made[0].printed) == 2
