from xr18_mcp import nodes

# Lines captured from a live XR18 (firmware 1.25).
CAPTURED = (
    '/ch/01/config "Guitar" 2 In01 U01\n'
    "/ch/01/mix ON   -oo ON 0.0000\n"
    '/ch/02/config "" 0 In02 U02\n'
    "/ch/01/mix/02   -oo OFF POSTEQ\n"
    "garbage without slash\n"
    "\n"
)


def test_tokenize_keeps_quoted_and_empty_strings():
    assert nodes.tokenize('/a "two words" "" x') == ["/a", "two words", "", "x"]


def test_parse_lines():
    lines = nodes.parse_lines(CAPTURED)
    assert lines["ch/01/config"] == ["Guitar", "2", "In01", "U01"]
    assert lines["ch/02/config"][0] == ""
    assert "garbage" not in " ".join(lines)
    assert len(lines) == 4


def test_named_with_layout_and_short_rows():
    lines = nodes.parse_lines(CAPTURED)
    assert nodes.named("ch/01/mix", lines["ch/01/mix"], nodes.layout_of("ch/01/mix")) == {
        "on": "ON", "fader": "-oo", "lr": "ON", "pan": "0.0000",
    }
    # even bus sends have no pan value
    assert nodes.layout_of("ch/01/mix/02") == ("level", "grpon", "tap")
    assert nodes.named("x", ["1"], ("a", "b")) == {"a": "1"}


def test_layout_of_unknown_path():
    assert nodes.layout_of("nope/1") is None
