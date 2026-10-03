"""Command line entry point: python -m runapp {login,sync,serve,demo}."""

import argparse
import logging

from . import config, db


def use_demo_data():
    config.DEMO = True
    config.DATA_DIR = config.DATA_DIR / "demo"
    config.DB_PATH = config.DATA_DIR / "runapp.db"


def main():
    parser = argparse.ArgumentParser(prog="runapp", description="Garmin running log and analysis")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("login", help="Log in to Garmin Connect and save session tokens")
    p_sync = sub.add_parser("sync", help="Download new activities from Garmin")
    p_sync.add_argument("--full", action="store_true", help="Re-check the entire activity history")
    p_serve = sub.add_parser("serve", help="Start the dashboard on http://localhost:8000")
    p_serve.add_argument("--port", type=int, default=8000)
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
        from . import sync

        client = sync.login(interactive=True)
        print(f"Logged in as {client.full_name or client.display_name}. Tokens saved to {config.TOKEN_PATH}")
    elif args.cmd == "sync":
        from . import sync

        client = sync.login(interactive=True)
        sync.run_sync(db.connect(), client, full=args.full)
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
        from .server import app

        if args.keep_awake:
            import os
            import subprocess

            # caffeinate exits by itself when this process does.
            subprocess.Popen(["caffeinate", "-ims", "-w", str(os.getpid())])
            print("Keeping the Mac awake while the app runs.")
        print(f"Dashboard: http://localhost:{args.port}  (data: {config.DB_PATH})")
        uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
