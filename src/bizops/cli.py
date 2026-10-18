"""`bizops` command line: serve, seed, tick, pause/resume, fund, payee, status."""

from __future__ import annotations

import argparse
import asyncio
import sys

from .config import Settings
from .core import Core


def _core(args: argparse.Namespace) -> Core:
    settings = Settings.load(db_path=getattr(args, "db", None), dry_run=getattr(args, "dry_run", None) or None)
    return Core(settings).init()


def _bootstrap_security(core) -> str | None:
    """BIZOPS_PASSWORD (e.g. from a deploy secret) becomes the owner login. Returns a new password if one
    had to be generated for a public bind."""
    import os
    import secrets

    from .web.auth import set_password
    env = core.settings.home / ".env"
    if os.environ.get("BIZOPS_PASSWORD") and not os.environ.get("BIZOPS_PASSWORD_HASH"):
        set_password(env, os.environ["BIZOPS_PASSWORD"])
    return None


def cmd_serve(args: argparse.Namespace) -> None:
    import asyncio as _asyncio
    import os
    import secrets

    import uvicorn

    from .engine import set_setting
    from .web.app import create_app
    from .web.auth import set_password
    core = _core(args)
    _bootstrap_security(core)
    if os.environ.get("FLY_APP_NAME") and not os.environ.get("PUBLIC_BASE_URL"):
        os.environ["PUBLIC_BASE_URL"] = f"https://{os.environ['FLY_APP_NAME']}.fly.dev"
    if os.environ.get("PUBLIC_BASE_URL"):
        set_setting(core.db, "public_base_url", os.environ["PUBLIC_BASE_URL"].rstrip("/"))
    public = args.host not in ("127.0.0.1", "localhost", "::1") or args.tunnel
    new_pw = None
    if public and not os.environ.get("BIZOPS_PASSWORD_HASH"):
        new_pw = secrets.token_urlsafe(14)
        set_password(core.settings.home / ".env", new_pw)
    from .seed import ensure_platform, seed_venture
    seed_venture(core) if args.dry_run or args.seed else ensure_platform(core)
    mode = ("DRY-RUN (scripted model, no tokens spent)" if core.backend == "dry_run"
            else f"LIVE via {core.backend}") + f" - {core.backend_why}"
    print(f"bizops: {mode}\n  db: {core.settings.db_path}\n  dashboard: http://{args.host}:{args.port}")
    if new_pw:
        print(f"  owner password (generated, saved to .env): {new_pw}")
    app = create_app(core, run_scheduler=not args.no_scheduler)

    async def main() -> None:
        server = uvicorn.Server(uvicorn.Config(app, host=args.host, port=args.port, log_level="warning"))
        proc = None
        if args.tunnel:
            from .tunnel import start_quick_tunnel
            proc, url = await start_quick_tunnel(args.port)
            if url:
                set_setting(core.db, "public_base_url", url)
                print(f"  public URL (Cloudflare quick tunnel): {url}")
            else:
                print("  tunnel: install cloudflared for a public URL (brew install cloudflared)", file=sys.stderr)
        try:
            await server.serve()
        finally:
            if proc:
                proc.terminate()
    _asyncio.run(main())


def cmd_seed(args: argparse.Namespace) -> None:
    from .seed import seed_demo
    print(seed_demo(_core(args)))


def cmd_tick(args: argparse.Namespace) -> None:
    from .scheduler import Scheduler

    async def go() -> None:
        core = _core(args)
        await Scheduler(core).drain(timeout=args.timeout)
        await core.aclose()
    asyncio.run(go())


def cmd_pause(args: argparse.Namespace, paused: bool) -> None:
    from .engine import set_paused
    core = _core(args)
    target = "platform" if args.target == "all" else args.target
    set_paused(core.db, target, args.id, paused)
    print(f"{'paused' if paused else 'resumed'} {target} {args.id or ''}".strip())


def cmd_fund(args: argparse.Namespace) -> None:
    from .payments import deposit
    core = _core(args)
    deposit(core.db, org_id=args.org, amount_usd=args.amount, memo=args.memo)
    print(f"deposited ${args.amount} into {args.org}")


def cmd_payee(args: argparse.Namespace) -> None:
    from .payments import create_payee
    core = _core(args)
    pid = create_payee(core.db, name=args.name, org_id=args.org, max_per_payment_usd=args.max, notes=args.notes)
    print(pid)


def cmd_install_service(args: argparse.Namespace) -> None:
    from pathlib import Path

    from .service import install, uninstall
    if args.uninstall:
        uninstall()
        print("bizops service removed")
        return
    where = install(Path(args.home or Path.cwd()).resolve(), port=args.port)
    print(f"bizops runs in the background now ({where}). Dashboard: http://127.0.0.1:{args.port}")


def cmd_maintain(args: argparse.Namespace) -> None:
    from .maintenance import run_jobs
    core = _core(args)
    print(run_jobs(core.db, core.settings, force=True))


def cmd_status(args: argparse.Namespace) -> None:
    from . import ledger
    from .engine import platform_paused
    from .util import fmt_usd, month_start
    core = _core(args)
    db = core.db
    print(f"platform paused: {platform_paused(db)}")
    for o in db.all("SELECT * FROM orgs ORDER BY created_at"):
        n = db.scalar("SELECT COUNT(*) AS n FROM agents WHERE org_id = ?", (o["id"],))
        print(f"{o['id']}  {o['name']:<24} {o['status']:<8} agents={n} "
              f"spend={fmt_usd(ledger.spent(db, since=month_start(), org_id=o['id']))}/{fmt_usd(o['monthly_cap_micros'])} "
              f"wallet={fmt_usd(ledger.wallet_balance(db, o['id']))}")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="bizops")
    p.add_argument("--db", help="SQLite path (default data/bizops.db or $BIZOPS_DB)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="API + scheduler + dashboard in one process")
    s.add_argument("--dry-run", action="store_true", help="scripted model, seeded demo company, $0")
    s.add_argument("--seed", action="store_true", help="also seed the demo company in live mode")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--no-scheduler", action="store_true")
    s.add_argument("--tunnel", action="store_true", help="public URL from this computer via a Cloudflare quick tunnel")
    s.set_defaults(fn=cmd_serve)

    s = sub.add_parser("set-password", help="set the owner login password for the dashboard")
    s.add_argument("password")
    s.set_defaults(fn=lambda a: (__import__("bizops.web.auth", fromlist=["set_password"]).set_password(
        _core(a).settings.home / ".env", a.password), print("password set")))

    s = sub.add_parser("seed", help="create the platform org and the demo company")
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(fn=cmd_seed)

    s = sub.add_parser("tick", help="run the scheduler until nothing is due")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--timeout", type=float, default=60)
    s.set_defaults(fn=cmd_tick)

    for name, paused in (("pause", True), ("resume", False)):
        s = sub.add_parser(name, help=f"kill switch: {name} an agent, an org, or everything")
        s.add_argument("target", choices=["agent", "org", "all"])
        s.add_argument("id", nargs="?")
        s.set_defaults(fn=lambda a, _p=paused: cmd_pause(a, _p))

    s = sub.add_parser("fund", help="owner deposit into an org wallet")
    s.add_argument("org")
    s.add_argument("amount", type=float)
    s.add_argument("--memo", default="owner deposit")
    s.set_defaults(fn=cmd_fund)

    s = sub.add_parser("payee", help="owner: allowlist a payee")
    s.add_argument("name")
    s.add_argument("--org", default=None, help="org id (omit for all orgs)")
    s.add_argument("--max", type=float, default=0, help="max per payment in USD (0 = policy max)")
    s.add_argument("--notes", default="")
    s.set_defaults(fn=cmd_payee)

    s = sub.add_parser("install-service", help="run bizops in the background at login (systemd/launchd/Windows)")
    s.add_argument("--home", default=None, help="folder with config/ and data/ (default: current folder)")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--uninstall", action="store_true")
    s.set_defaults(fn=cmd_install_service)

    s = sub.add_parser("maintain", help="run the background jobs once now (goal reviews, nudges, audit checks)")
    s.set_defaults(fn=cmd_maintain)

    s = sub.add_parser("status", help="orgs, spend and wallets")
    s.set_defaults(fn=cmd_status)

    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
