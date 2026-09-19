"""CLI entry point for anki-connect-server."""

import argparse
import sys


def main(argv: list[str] | None = None) -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    parser = argparse.ArgumentParser(
        prog="anki-connect-server",
        description="Headless AnkiConnect-compatible REST API server with MCP support",
    )
    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # API server subcommand
    subparsers.add_parser(
        "api",
        help="Run the AnkiConnect API server",
        description="Run the headless AnkiConnect-compatible REST API server.",
    )

    # MCP server subcommand
    mcp_parser = subparsers.add_parser(
        "mcp",
        help="Run the MCP server",
        description="Run the Model Context Protocol server for AI assistants.",
    )
    mcp_parser.add_argument(
        "--transport",
        choices=["stdio", "http"],
        default="stdio",
        help="Transport protocol: stdio (one process per client) or http "
        "(single long-lived server serving all clients). Default: stdio.",
    )

    subparsers.add_parser(
        "measure-loudness",
        help="Measure the collection's audio loudness and store it as the auto target",
    )

    normalize_parser = subparsers.add_parser(
        "normalize-media",
        help="Loudness-normalize audio already in the media folder to the configured target",
    )
    normalize_parser.add_argument(
        "--prefix", default="asbp_", help="Only files starting with this prefix (default: asbp_)"
    )
    normalize_parser.add_argument("--all", action="store_true", help="Normalize every audio file")
    normalize_parser.add_argument("--dry-run", action="store_true", help="Measure only")

    args = parser.parse_args(argv)

    if args.command == "api":
        from anki_connect_server.api import run_server

        run_server()
    elif args.command == "mcp":
        from anki_connect_server.mcp_server import run

        run(transport=args.transport)
    elif args.command == "measure-loudness":
        from anki_connect_server.api import create_anki_wrapper

        wrapper = create_anki_wrapper()
        try:
            target = wrapper.measure_and_store_audio_target()
        finally:
            wrapper.close()
        if target is None:
            sys.exit("No measurable audio files found in the media directory")
        sys.stdout.write(f"Stored audio normalization target: {target:.1f} LUFS\n")
    elif args.command == "normalize-media":
        from anki_connect_server.api import create_anki_wrapper

        wrapper = create_anki_wrapper()
        try:
            results = wrapper.normalize_existing_media(
                "" if args.all else args.prefix, args.dry_run
            )
        finally:
            wrapper.close()
        changed = 0
        for item in results:
            before = f"{item.before_lufs:.1f}" if item.before_lufs is not None else "n/a"
            after = f"{item.after_lufs:.1f}" if item.after_lufs is not None else "n/a"
            marker = " " if item.before_lufs == item.after_lufs else "*"
            changed += marker == "*"
            sys.stdout.write(f"{marker} {item.name}: {before} -> {after} LUFS\n")
        verb = "would change" if args.dry_run else "changed"
        sys.stdout.write(f"{len(results)} files, {changed} {verb}\n")
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
