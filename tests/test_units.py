import pytest

from xr18_mcp import units


@pytest.mark.parametrize("db", [-89, -80, -45.5, -30, -20, -10, -3, 0, 4.5, 9.9])
def test_fader_round_trip(db):
    assert units.fader_to_db(units.db_to_fader(db)) == pytest.approx(db, abs=1e-6)


@pytest.mark.parametrize("raw, db", [(1.0, 10.0), (1.5, 10.0), (0.75, 0.0), (0.5, -10.0), (0.25, -30.0), (0.0625, -60.0)])
def test_fader_segment_edges(raw, db):
    assert units.fader_to_db(raw) == pytest.approx(db)


def test_fader_extremes():
    assert units.fader_to_db(0.0) == units.NEG_INF
    assert units.fader_to_db(-0.1) == units.NEG_INF
    assert units.db_to_fader(12) == 1.0
    assert units.db_to_fader(-90) == 0.0
    assert units.db_to_fader(-200) == 0.0


def test_lin_and_log_mappings():
    assert units.lin_to(-15, 15, 0.5) == 0
    assert units.lin_from(-15, 15, 0) == 0.5
    assert units.log_to(20, 20000, 0) == pytest.approx(20)
    assert units.log_to(20, 20000, 1) == pytest.approx(20000)
    assert units.log_from(20, 20000, 632.456) == pytest.approx(0.5, abs=1e-4)
    # inverted log range (EQ Q runs 10 -> 0.3)
    assert units.log_to(10, 0.3, 0) == pytest.approx(10)
    assert units.log_from(10, 0.3, 0.3) == pytest.approx(1)


def test_clamp():
    assert units.clamp(5, 0, 3) == 3
    assert units.clamp(-5, 0, 3) == 0
    assert units.clamp(2, 0, 3) == 2


@pytest.mark.parametrize("text, hz", [("1k98", 1980), ("19k32", 19320), ("10k5", 10500), ("1k", 1000), ("129.1", 129.1)])
def test_parse_freq(text, hz):
    assert units.parse_freq(text) == pytest.approx(hz)


def test_parse_and_format_db():
    assert units.parse_db("-oo") == units.NEG_INF
    assert units.parse_db("-inf") == units.NEG_INF
    assert units.parse_db("+0.6") == 0.6
    assert units.fmt_db(units.NEG_INF) == "-inf"
    assert units.fmt_db(-0.04) == "0.0"  # no "-0.0"
    assert units.fmt_db(3.14) == "+3.1"
    assert units.fmt_db(-26.25) == "-26.2"


def test_fmt_freq():
    assert units.fmt_freq(129.4) == "129"
    assert units.fmt_freq(2500) == "2.50k"
