from PIL import Image

from p21studio import protocol


def test_crc_matches_reference_vector():
    # vector from julienvincent/nelko-p21-rs, captured from a real printer
    assert protocol.crc16(bytes.fromhex("200c011203000301121215280f0e")) == bytes([0x0D, 0x22])


def test_parse_status_real_capture():
    st = protocol.parse_status(bytes.fromhex("200c011203000301121215280f0e0d22"))
    assert st.readiness == protocol.Readiness.BUSY
    assert (st.label_width_mm, st.label_length_mm) == (14, 40)
    assert st.label_color == "white" and st.paper_type == "gapped"
    # response observed after a print job (KalebCole hardware log)
    after = protocol.parse_status(bytes.fromhex("000c011203000301121215280f0eed03"))
    assert after.readiness == protocol.Readiness.READY


def test_build_status_roundtrip():
    frame = protocol.build_status(0)
    assert frame.hex() == "000c011203000301121215280f0eed03"


def test_parse_config_and_battery():
    cfg = protocol.parse_config(bytes.fromhex("434f4e4649472000cb00000304020402010d0a"))
    assert cfg.dpi == 203 and cfg.hardware_version == "0.0.3" and cfg.firmware_version == "4.2.4"
    assert cfg.auto_off_minutes == 30 and cfg.beep is True
    bat = protocol.parse_battery(bytes.fromhex("424154544552592075000d0a"))  # "BATTERY 0x75 0x00"
    assert bat.percent == 75 and not bat.charging


def test_job_is_byte_identical_to_hardware_verified_layout():
    img = Image.new("1", (96, 284), 1)
    bmp = protocol.pack_bitmap(img)
    job = protocol.build_print_job(bmp, 284)
    assert len(job) == 3515  # exact size of the job proven on hardware
    assert job.startswith(b"\x1b!o\r\nSIZE 14.0 mm,40.0 mm\r\nGAP 5.0 mm,0 mm\r\nDIRECTION 1,1\r\nDENSITY 15\r\nCLS\r\nBITMAP 0,0,12,284,1,")
    assert job.endswith(b"\r\nPRINT 1\r\n")


def test_bitmap_polarity_and_bit_order():
    img = Image.new("L", (96, 284), 255)
    for x in (0, 3, 7):
        img.putpixel((x, 0), 0)
    data = protocol.pack_bitmap(img)
    assert len(data) == 3408
    assert data[0] == 0b0110_1110  # MSB first, 0 = black
    assert all(b == 0xFF for b in data[1:])


def test_length_dots():
    assert protocol.length_dots(40) == 284
    assert protocol.length_dots(30) == 213
    assert protocol.length_dots(50) == 355


def test_settings_commands_use_raw_bytes():
    assert protocol.beep_command(False) == b"BEEP \x00\r\n"
    assert protocol.timeout_command(60) == b"TIMEOUT \x03\r\n"
