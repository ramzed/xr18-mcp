"""MCP server for the Behringer XR18 digital mixer."""


def main() -> None:
    from .server import main as _main

    _main()
