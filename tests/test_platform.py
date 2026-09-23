import re
from pathlib import Path

from xr18_mcp import osc_client, server


def test_default_data_dir_avoids_macos_documents(monkeypatch, tmp_path):
    (tmp_path / "Documents").mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(server.sys, "platform", "darwin")
    assert server._default_data_dir() == tmp_path / "XR18-MCP"
    monkeypatch.setattr(server.sys, "platform", "win32")
    assert server._default_data_dir() == tmp_path / "Documents" / "XR18-MCP"


def test_subnet_broadcast_shape():
    b = osc_client._subnet_broadcast()
    assert b is None or re.fullmatch(r"\d+\.\d+\.\d+\.255", b)


def test_discover_survives_send_errors(monkeypatch):
    class BadSocket:
        def __init__(self, *a):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def setsockopt(self, *a):
            pass

        def settimeout(self, *a):
            pass

        def connect(self, *a):
            raise OSError(51, "Network is unreachable")

        def sendto(self, *a):
            raise OSError(65, "No route to host")

        def recvfrom(self, *a):
            raise TimeoutError

    monkeypatch.setattr(osc_client.socket, "socket", BadSocket)
    assert osc_client.discover(timeout=0.1) == []
