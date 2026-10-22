import json

import pytest
from PIL import Image, ImageChops

from p21studio import api, label as labelmod
from p21studio.cli import main
from p21studio.printer import P21, PrinterNotReady
from p21studio.render import render, text_label
from p21studio.transports import MockTransport, from_spec, RfcommTransport, SerialTransport, BleTransport


def test_landscape_is_rotated_clockwise_for_the_printer():
    lab = labelmod.new_label("14x40", [{"type": "rect", "x": 0, "y": 0, "w": 10, "h": 10, "fill": True}])
    r = render(lab)
    assert r.size == (284, 96)
    p = r.printer_image()
    assert p.size == (96, 284)
    # top-left of the design must become the top-right of the printer bitmap (clockwise)
    assert p.getpixel((95, 0)) == 0 and p.getpixel((0, 0)) == 255


def test_mock_printer_roundtrip_matches_design(tmp_path):
    lab = text_label("往復テスト")
    r = render(lab)
    mock = MockTransport(str(tmp_path))
    with P21(mock) as p:
        res = p.print_image(r.printer_image())
    assert res.ok and res.acknowledged and res.finished
    printed = Image.open(res.mock_png[0]).convert("L")
    assert ImageChops.difference(printed, r.image).getbbox() is None


def test_not_ready_is_reported_before_sending(tmp_path):
    mock = MockTransport(str(tmp_path))
    mock._readiness = lambda: 1  # lid open
    with P21(mock) as p, pytest.raises(PrinterNotReady):
        p.print_image(render(text_label("x")).printer_image())
    assert not mock.jobs


def test_normalize_reports_errors():
    with pytest.raises(labelmod.LabelError) as e:
        labelmod.normalize({"elements": [{"type": "text", "x": 0, "y": 0, "w": 0, "h": 5, "align": "middle"}]})
    assert "align" in str(e.value) and "elements[0].w" in str(e.value)


def test_variables_and_csv_expansion():
    lab = labelmod.normalize({"elements": [{"type": "text", "x": 0, "y": 0, "w": 100, "h": 40, "text": "{{name}} #{{n:03}}"}]})
    labels = api.expand(lab, rows=[{"name": "A"}, {"name": "B"}])
    assert [l["elements"][0]["text"] for l in labels] == ["A #001", "B #002"]
    assert labelmod.find_variables(lab) == ["name"]


def test_render_warnings_for_impossible_content():
    lab = {"elements": [
        {"type": "qr", "x": 0, "y": 0, "w": 10, "h": 10, "data": "https://example.com/very/long/url"},
        {"type": "text", "x": 0, "y": 0, "w": 5, "h": 5, "text": "長い文章が入らない"},
        {"type": "barcode", "x": 250, "y": 0, "w": 60, "h": 90, "data": "123"},
    ]}
    w = "\n".join(render(lab).warnings)
    assert "qr" in w and "text" in w and "overflows" in w


def test_all_element_types_render():
    r = render({"elements": [
        {"type": "text", "x": 2, "y": 2, "w": 80, "h": 40, "text": "縦書き", "vertical": True},
        {"type": "barcode", "x": 90, "y": 2, "w": 190, "h": 50, "data": "4901234567894", "symbology": "jan"},
        {"type": "ellipse", "x": 2, "y": 50, "w": 40, "h": 40, "fill": True},
        {"type": "line", "x1": 50, "y1": 60, "x2": 280, "y2": 90, "width": 3},
        {"type": "rect", "x": 0, "y": 0, "w": 284, "h": 96, "stroke": 2, "radius": 8},
    ]})
    assert r.warnings == []
    assert r.image.getbbox() is not None


def test_device_specs():
    assert isinstance(from_spec("serial:COM5"), SerialTransport)
    assert isinstance(from_spec("COM7"), SerialTransport)
    assert isinstance(from_spec("ble:AA:BB:CC:DD:EE:FF"), BleTransport)
    assert isinstance(from_spec("mock:"), MockTransport)
    t = from_spec("rfcomm:aa:bb:cc:dd:ee:ff@2")
    assert isinstance(t, RfcommTransport) and t.channel == 2 and t.address == "AA:BB:CC:DD:EE:FF"


def test_cli_render_and_print(tmp_path, capsys):
    f = tmp_path / "l.p21.json"
    f.write_text(json.dumps(labelmod.EXAMPLE, ensure_ascii=False), encoding="utf-8")
    assert main(["render", str(f), "-o", str(tmp_path / "o.png"), "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["canvas"] == {"width": 284, "height": 96} and (tmp_path / "o.png").exists()
    assert main(["print", str(f), "-d", f"mock:{tmp_path}", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["printed"] == 1
    assert main(["print", "-t", "x", "-d", "nonsense", "--json"]) == 4
