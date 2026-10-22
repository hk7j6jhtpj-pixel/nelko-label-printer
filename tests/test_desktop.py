"""Desktop app helpers: label building, history file, colour contrast, formatting."""

from p21studio import api, desktop


def job(**kw):
    return {"text": "Flour", "font": "auto", "size": "Auto", "bold": False, "italic": False, "underline": False, "copies": 1, **kw}


def ink(j) -> int:
    """Number of black dots in the rendered label."""
    return api.render_label(desktop.build_label(j)).image.histogram()[0]


def test_build_label_is_one_full_text_element():
    lab = desktop.build_label(job(size="32", bold=True))
    (el,) = lab["elements"]
    assert el["type"] == "text" and el["size"] == 32 and el["bold"] is True
    assert lab["media"] == {"width_mm": 14, "length_mm": 40}


def test_auto_size_spellings():
    for s in ("Auto", "auto", "", None):
        assert desktop.build_label(job(size=s))["elements"][0]["size"] == "auto"


def test_underline_and_italic_change_the_print():
    plain = api.render_label(desktop.build_label(job(size="40"))).image
    under = api.render_label(desktop.build_label(job(size="40", underline=True))).image
    slant = api.render_label(desktop.build_label(job(size="40", italic=True))).image
    assert under.tobytes() != plain.tobytes()
    assert slant.tobytes() != plain.tobytes()
    assert ink(job(size="40", underline=True)) > ink(job(size="40"))


def test_formatting_keys_do_not_warn():
    res = api.render_label(desktop.build_label(job(italic=True, underline=True)))
    assert not [w for w in res.warnings if "italic" in w or "underline" in w]


def test_history_newest_first_and_capped(tmp_path):
    path = tmp_path / "history.json"
    assert desktop.load_history(path) == []
    for i in range(desktop.HISTORY_MAX + 5):
        items = desktop.add_history(job(text=f"label {i}", copies=2), path)
    assert len(items) == desktop.HISTORY_MAX
    assert items[0]["text"] == f"label {desktop.HISTORY_MAX + 4}"
    assert items[0]["copies"] == 2 and items[0]["printed_at"]
    assert desktop.load_history(path) == items


def test_history_survives_a_corrupt_file(tmp_path):
    path = tmp_path / "history.json"
    path.write_text("not json", encoding="utf-8")
    assert desktop.load_history(path) == []


def test_on_color_picks_readable_text():
    assert desktop.on_color("#FFFFFF") == "#0F172A"
    assert desktop.on_color("#1E3A5F") == "#FFFFFF"
    assert desktop.on_color("#059669") == "#0F172A"


def test_print_through_mock_printer(tmp_path, monkeypatch):
    monkeypatch.setenv("P21_DEVICE", f"mock:{tmp_path}")
    res = api.print_labels(desktop.build_label(job(text="Rice", bold=True)), copies=2)
    assert res["ok"] and res["sheets"] == 2
    assert list(tmp_path.glob("*.png"))


def test_icon_font_has_every_icon():
    from PIL import ImageFont

    font = ImageFont.truetype(str(desktop.ICON_FONT), 24)
    for name, cp in desktop.ICONS.items():
        assert font.getbbox(chr(cp))[2] > 0, name


def test_battery_low_threshold():
    assert desktop.battery_is_low(desktop.LOW_BATTERY_PERCENT)
    assert desktop.battery_is_low(5)
    assert not desktop.battery_is_low(desktop.LOW_BATTERY_PERCENT + 1)
    assert not desktop.battery_is_low(None)  # unknown is not shown as low
