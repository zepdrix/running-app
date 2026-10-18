"""Command line entry point: python -m runapp {login,sync,serve,demo}."""

import argparse
import logging
import os
import threading
import urllib.request
import webbrowser

from . import config, db


def use_demo_data():
    config.DEMO = True
    config.DATA_DIR = config.DATA_DIR / "demo"
    config.DB_PATH = config.DATA_DIR / "runapp.db"


def _already_running(port: int) -> bool:
    """Whether this app is already answering on the port."""
    try:
        with urllib.request.urlopen(f"http://localhost:{port}/manifest.webmanifest", timeout=2) as res:
            return b"Running Log" in res.read()
    except OSError:
        return False


def main():
    parser = argparse.ArgumentParser(prog="runapp", description="Running log and analysis for Garmin or Strava")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("login", help="Log in to Garmin Connect in the terminal (the app's setup page does the same in the browser)")
    p_sync = sub.add_parser("sync", help="Download new activities from Garmin or Strava, whichever is connected")
    p_sync.add_argument("--full", action="store_true", help="Re-check the entire activity history")
    p_serve = sub.add_parser("serve", help="Start the dashboard on http://localhost:8000")
    p_serve.add_argument("--port", type=int, default=int(os.getenv("RUNAPP_PORT", "8000")))
    p_serve.add_argument("--open", action="store_true", help="Open the app in your browser once it's running")
    p_serve.add_argument("--demo", action="store_true", help="Serve the synthetic demo database")
    p_serve.add_argument("--keep-awake", action="store_true", help="Stop the Mac from sleeping while the app runs (for phone access)")
    sub.add_parser("demo", help="Create a synthetic demo database (data/demo/)")
    p_pw = sub.add_parser("password", help="Set the password needed to open the app (for phone access)")
    p_pw.add_argument("--remove", action="store_true", help="Remove the password (only safe if the app is local-only)")
    sub.add_parser("reparse", help="Re-read every saved FIT file (after an upgrade adds new fields); no downloads")
    p_query = sub.add_parser("query", help="Run a read-only SQL query against the database")
    p_query.add_argument("sql")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    if args.cmd == "login":
        from . import source, sync

        if source.current() == "strava":
            raise SystemExit("This app is connected to Strava. To use Garmin instead, disconnect Strava on the app's Connection page.")
        client = sync.login(interactive=True)
        print(f"Logged in as {client.full_name or client.display_name}. Tokens saved to {config.TOKEN_PATH}")
    elif args.cmd == "sync":
        from . import source, sync

        if source.current() == "strava":
            from . import strava

            strava.run_sync(db.connect(), strava.Client(), full=args.full)
        else:
            sync.run_sync(db.connect(), sync.login(interactive=True), full=args.full)
    elif args.cmd == "demo":
        from . import demo

        use_demo_data()
        if config.DB_PATH.exists():
            config.DB_PATH.unlink()
        n = demo.generate(db.connect())
        print(f"Created {n} demo runs in {config.DB_PATH}. Run: .venv/bin/python -m runapp serve --demo")
    elif args.cmd == "password":
        import getpass

        from . import auth

        conn = db.connect()
        if args.remove:
            auth.remove_password(conn)
            print("Password removed. Anyone who can reach the app can now open it.")
            return
        pw = getpass.getpass("New password: ")
        if len(pw) < 8:
            raise SystemExit("Use at least 8 characters.")
        if getpass.getpass("Repeat it: ") != pw:
            raise SystemExit("Passwords didn't match; nothing changed.")
        auth.set_password(conn, pw)
        print("Password set. Every device (including this Mac's browser) will need to log in.")
    elif args.cmd == "reparse":
        from . import reparse

        reparse.run(db.connect())
    elif args.cmd == "query":
        from . import query

        query.main([args.sql])
    elif args.cmd == "serve":
        import uvicorn

        if args.demo:
            use_demo_data()
        url = f"http://localhost:{args.port}"
        if _already_running(args.port):
            # Double-clicked again while it's running: just bring the app up in the browser.
            print(f"Running Log is already running at {url}")
            if args.open:
                webbrowser.open(url)
            return
        from .server import app

        if args.keep_awake:
            import subprocess

            # caffeinate exits by itself when this process does.
            subprocess.Popen(["caffeinate", "-ims", "-w", str(os.getpid())])
            print("Keeping the Mac awake while the app runs.")
        print(f"Running Log is at {url}  (data: {config.DB_PATH})")
        if args.open:
            print("Leave this window open while you use the app. Close it to stop the app.")
            threading.Timer(1.0, webbrowser.open, args=(url,)).start()
        uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
