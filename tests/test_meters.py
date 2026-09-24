import struct

import pytest

from xr18_mcp import meters
from xr18_mcp.model import Strip

from .fake_mixer import meter_blob


def test_decode():
    assert meters.decode(meter_blob([-12.5, 0.0, -128.0])) == [-12.5, 0.0, -128.0]
    assert meters.decode(b"\x01") == []
    # a count larger than the payload is clipped to what is there
    assert meters.decode(struct.pack("<i", 10) + struct.pack("<2h", 256, -256)) == [1.0, -1.0]


def test_bank_layouts_match_documented_sizes():
    for scope, count in (("channels", 40), ("inputs", 36)):
        _, layout = meters.BANKS[scope]
        idx = sorted(i for _, _, idxs in layout for i in idxs)
        assert idx == list(range(count))
    _, layout = meters.BANKS["channels"]
    by_id = {ident: (strip, idxs) for ident, strip, idxs in layout}
    assert by_id["ch16"] == (Strip("ch", 16), [15])
    assert by_id["fxrtn4"][1] == [24, 25]
    assert by_id["bus1"][1] == [26] and by_id["fxsend4"][1] == [35]
    assert by_id["lr"][1] == [36, 37] and by_id["monitor"] == (None, [38, 39])


@pytest.mark.parametrize(
    "peak, status",
    [(-128, "digital silence (no source)"), (-100, "no signal (noise floor)"), (0, "CLIPPING"), (-3, "hot"),
     (-50, "low"), (-18, "ok")],
)
def test_level_status(peak, status):
    assert meters.Level("x", None, peak, peak).status == status


def test_analyse_peak_avg_and_stereo_pairs():
    layout = [("ch1", Strip("ch", 1), [0]), ("lr", Strip("lr"), [1, 2]), ("gone", None, [9])]
    frames = [meter_blob([-20.0, -30.0, -10.0]), meter_blob([-6.0, -40.0, -12.0]), b"\x00"]
    levels = {lv.id: lv for lv in meters.analyse(frames, layout)}
    assert levels["ch1"].peak == -6.0
    assert -20.0 < levels["ch1"].avg < -6.0  # average of linear amplitude
    assert levels["lr"].peak == -10.0  # max of L/R
    assert "gone" not in levels  # index beyond the frame


def test_analyse_all_silent():
    lv = meters.analyse([meter_blob([-128.0])], [("x", None, [0])])[0]
    assert lv.avg == pytest.approx(-128.0, abs=0.1)
