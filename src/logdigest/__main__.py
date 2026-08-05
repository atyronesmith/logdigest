"""logdigest CLI — stdlib dispatcher for optional subcommands.

The base package has no runtime dependencies. Subcommands lazy-import
their extras and fail with clear install instructions when missing.

    logdigest discover ...   requires [discover] (click, drain3)
    logdigest serve          requires [mcp] (fastmcp)
"""

from __future__ import annotations

import sys


def main() -> None:
    args = sys.argv[1:]

    if not args or args[0] in ("-h", "--help"):
        _print_help()
        return

    if args[0] in ("-V", "--version"):
        _print_version()
        return

    cmd = args[0]

    if cmd == "discover":
        _run_discover()
    elif cmd == "serve":
        _run_serve()
    else:
        print(f"Unknown command: {cmd}\n", file=sys.stderr)
        _print_help()
        raise SystemExit(1)


def _run_discover() -> None:
    try:
        from logdigest.cli import cli
    except ImportError:
        print(
            "Error: click is required for pattern discovery.\n"
            "Install with: pip install logdigest[discover]",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
    cli()


def _run_serve() -> None:
    try:
        from logdigest.mcp import main as mcp_main
    except ImportError:
        print(
            "Error: fastmcp is required for the MCP server.\n"
            "Install with: pip install logdigest[mcp]",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
    mcp_main()


def _print_version() -> None:
    try:
        from importlib.metadata import version
        print(f"logdigest {version('logdigest')}")
    except Exception:
        print("logdigest (version unknown — not installed as package)")


def _print_help() -> None:
    print(
        "logdigest — log pre-digestion toolkit\n"
        "\n"
        "Commands:\n"
        "  discover    Discover noise/signal patterns from a log corpus  [discover]\n"
        "  serve       Start the MCP server for Claude Code integration  [mcp]\n"
        "\n"
        "Options:\n"
        "  -h, --help     Show this help\n"
        "  -V, --version  Show version\n"
        "\n"
        "Brackets show the optional extra required for each command.\n"
        "The core library requires no extras — use it via Python import."
    )


if __name__ == "__main__":
    main()
