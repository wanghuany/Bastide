"""Run the personal edition on loopback with no Flutter or external service required."""

import argparse
import os
import socket
import threading
import webbrowser
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description="Bastide Personal: local encrypted ledger")
    parser.add_argument("--port", type=int, default=8899)
    parser.add_argument("--vault", type=Path)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("port must be between 1024 and 65535")
    # Original ORM imports initialise a default engine. It must never open a disk datastore
    # in personal mode; our actual connection is created by the encrypted Vault.
    os.environ["BASTIDE_DB_PATH"] = ":memory:"
    import uvicorn

    from app.personal import create_personal_app

    app = create_personal_app(args.vault)
    config = uvicorn.Config(
        app, host="127.0.0.1", port=args.port, access_log=False, log_level="warning"
    )
    server = uvicorn.Server(config)
    address = f"http://127.0.0.1:{args.port}"
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            listener.bind(("127.0.0.1", args.port))
        except OSError:
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
            "Keep this terminal open. Press Ctrl+C to stop."
        )
        server.run(sockets=[listener])


if __name__ == "__main__":
    main()
