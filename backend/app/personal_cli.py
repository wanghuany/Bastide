"""Run the personal edition on loopback with no Flutter or external service required."""

import argparse
import os
import socket
import sys
import threading
import webbrowser
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description="Bastide Personal: local encrypted ledger")
    parser.add_argument("--port", type=int, default=8899)
    parser.add_argument("--vault", type=Path)
    parser.add_argument("--no-browser", action="store_true")
    lifetime_options = parser.add_mutually_exclusive_group()
    lifetime_options.add_argument("--keep-running", dest="exit_on_close", action="store_false")
    lifetime_options.add_argument("--exit-on-close", dest="exit_on_close", action="store_true")
    parser.set_defaults(exit_on_close=None)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("port must be between 1024 and 65535")
    # Original ORM imports initialise a default engine. It must never open a disk datastore
    # in personal mode; our actual connection is created by the encrypted Vault.
    os.environ["BASTIDE_DB_PATH"] = ":memory:"
    import uvicorn

    from app.features.personal.browser_lifetime import BrowserLifetime
    from app.personal import create_personal_app

    exit_on_close = args.exit_on_close if args.exit_on_close is not None else not args.no_browser

    def request_exit() -> None:
        print("All ledger pages closed. Stopping the local ledger safely.")
        server.should_exit = True

    lifetime = BrowserLifetime(request_exit) if exit_on_close else None
    app = create_personal_app(args.vault, browser_lifetime=lifetime)
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=args.port,
        access_log=False,
        log_level="warning",
        ws_ping_interval=5,
        ws_ping_timeout=5,
        log_config=uvicorn.config.LOGGING_CONFIG if sys.stderr is not None else None,
    )
    server = uvicorn.Server(config)
    app.state.request_exit = request_exit
    address = f"http://127.0.0.1:{args.port}"
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            listener.bind(("127.0.0.1", args.port))
        except OSError:
            import httpx

            try:
                with httpx.Client(trust_env=False, timeout=2) as client:
                    response = client.get(address + "/api/status")
                    if response.status_code == 200 and "idle_lock_minutes" in response.json():
                        if not args.no_browser:
                            webbrowser.open(address)
                        return
            except httpx.HTTPError, ValueError:
                pass
            parser.exit(1, f"Port {args.port} is in use. Choose a different --port.\n")
        listener.listen(128)
        listener.setblocking(False)
        if not args.no_browser:

            def open_browser() -> None:
                if server.started:
                    webbrowser.open(address)

            timer = threading.Timer(1.5, open_browser)
            timer.daemon = True
            timer.start()
        print(
            f"Bastide Personal: {address}\n"
            f"Encrypted ledger: {app.state.personal.vault.path}\n"
            + (
                "Closing the last ledger page stops this app after 5 seconds. "
                "If no page connects within 2 minutes, this app also stops."
                if exit_on_close
                else "Background mode: closing the page keeps this app running."
            )
            + "\nPress Ctrl+C to stop manually."
        )
        server.run(sockets=[listener])


if __name__ == "__main__":
    main()
