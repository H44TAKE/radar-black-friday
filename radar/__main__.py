"""CLI:  python -m radar run --auto | diagnose | export | test-notify"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from .collectors import build_collectors
from .config import ROOT, load_config
from .dashboard import export
from .db import DB
from .http import Blocked, Fetcher, RobotsDisallow
from .notify import Notifier
from .runner import App
from .util import brl


def make_app(args) -> App:
    cfg = load_config(args.config)
    db = DB(args.db)
    f = Fetcher(respect_robots=bool((cfg.get("settings") or {}).get("respect_robots", True)))
    return App(cfg, db, f, Notifier(dry_run=args.dry_run))


def cmd_run(args):
    app = make_app(args)
    tiers = ["priority", "general", "community"] if args.tier == "all" else [args.tier] if args.tier != "auto" else ["priority", "general", "community"]
    force = args.tier not in ("auto",)
    if not app.n.configured and not args.dry_run:
        print("AVISO: TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID não definidos — nenhuma notificação será enviada.")
    app.run_tiers(tiers, force=force or args.force)
    if app.db.job_last("prune") is None or (time.time() - app.db.job_last("prune").timestamp()) > 86400:
        app.db.prune((app.cfg.get("settings") or {}).get("history_days", 120))
        app.db.job_done("prune")
    export(app.db, app.cfg, args.out)
    print(f"checados={app.run.checked} alertas={app.run.alerts} erros={len(app.run.errors)}")
    app.db.close()


def cmd_export(args):
    app = make_app(args)
    export(app.db, app.cfg, args.out)
    app.db.close()


def cmd_notify(args):
    n = Notifier(dry_run=args.dry_run)
    ok = n.send("✅ <b>Radar Black Friday</b>: teste de notificação. Se você está lendo isto no celular, está tudo certo!")
    print("enviado" if ok else "FALHOU (confira TELEGRAM_BOT_TOKEN e TELEGRAM_CHAT_ID)")
    sys.exit(0 if ok else 1)


def cmd_diagnose(args):
    """Teste ao vivo de cada fonte. Imprime o que foi lido e salva trechos do HTML para ajustar seletores."""
    app = make_app(args)
    outdir = Path(args.out_dir)
    outdir.mkdir(parents=True, exist_ok=True)
    report = {"sources": {}}
    f, cfg = app.f, app.cfg
    print("=== Lojas: páginas de produto prioritárias ===")
    for p in cfg["products"]:
        for store, url in (p.get("urls") or {}).items():
            coll = app.collectors[store]
            row = {"url": url}
            try:
                r = f.get(url)
                (outdir / f"{store}-produto.html").write_text(r.text[:400000], encoding="utf-8")
                o = coll.offer_from_page(r.text, r.url)
                row.update(http=r.status_code, bytes=len(r.content), parsed=bool(o),
                           price=o.price if o else None, old_price=o.old_price if o else None,
                           in_stock=o.in_stock if o else None, title=o.title[:90] if o else None)
            except (Blocked, RobotsDisallow) as e:
                row.update(error=f"{type(e).__name__}: {e}")
            except Exception as e:  # noqa: BLE001
                row.update(error=f"{type(e).__name__}: {e}")
            report["sources"][f"{store}:produto"] = row
            print(f"- {store}: {json.dumps(row, ensure_ascii=False)}")
    print("\n=== Lojas: busca ===")
    for name, coll in app.collectors.items():
        if not coll.enabled():
            report["sources"][f"{name}:busca"] = {"status": "desativada (faltam credenciais)"}
            print(f"- {name}: desativada (faltam credenciais de API)")
            continue
        row = {}
        try:
            res = coll.search("water cooler lcd 360mm", limit=10)
            row.update(resultados=len(res), amostra=[{"t": o.title[:70], "preco": o.price, "estoque": o.in_stock} for o in res[:3]])
        except (Blocked, RobotsDisallow) as e:
            row.update(error=f"{type(e).__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            row.update(error=f"{type(e).__name__}: {e}")
        report["sources"][f"{name}:busca"] = row
        print(f"- {name}: {json.dumps(row, ensure_ascii=False)}")
    print("\n=== Telegram: canais públicos ===")
    from .sources import fetch_channel
    for ch in (cfg.get("telegram") or {}).get("channels") or []:
        h = ch["handle"] if isinstance(ch, dict) else ch
        row = {}
        try:
            posts = fetch_channel(f, h, respect_robots=bool(cfg["telegram"].get("respect_robots", True)))
            row.update(posts=len(posts), ultimo=posts[-1].text[:80] if posts else None,
                       ultimo_ts=posts[-1].ts.isoformat() if posts else None)
        except Exception as e:  # noqa: BLE001
            row.update(error=f"{type(e).__name__}: {e}")
        report["sources"][f"telegram:{h}"] = row
        print(f"- @{h}: {json.dumps(row, ensure_ascii=False)}")
    print("\n=== Instagram ===")
    if os.environ.get("IG_ACCESS_TOKEN") and (cfg.get("instagram") or {}).get("profiles"):
        from .sources import instagram_posts
        try:
            posts = instagram_posts(f, [p if isinstance(p, str) else p["username"] for p in cfg["instagram"]["profiles"]], limit=3)
            print(f"- {len(posts)} publicações lidas")
        except Exception as e:  # noqa: BLE001
            print(f"- erro: {e}")
    else:
        print("- não configurado (IG_USER_ID/IG_ACCESS_TOKEN + perfis)")
    (outdir / "diagnostico.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    app.db.close()


def main(argv=None):
    ap = argparse.ArgumentParser(prog="radar")
    ap.add_argument("--config", default=None)
    ap.add_argument("--db", default=os.environ.get("RADAR_DB", str(ROOT / "state" / "radar.sqlite")))
    ap.add_argument("--dry-run", action="store_true", help="não envia nada; imprime as mensagens")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--tier", default="auto", choices=["auto", "priority", "general", "community", "all"])
    r.add_argument("--force", action="store_true")
    r.add_argument("--out", default=str(ROOT / "state" / "data.json"))
    r.set_defaults(fn=cmd_run)
    e = sub.add_parser("export")
    e.add_argument("--out", default=str(ROOT / "state" / "data.json"))
    e.set_defaults(fn=cmd_export)
    sub.add_parser("test-notify").set_defaults(fn=cmd_notify)
    d = sub.add_parser("diagnose")
    d.add_argument("--out-dir", default=str(ROOT / "state" / "diagnostico"))
    d.set_defaults(fn=cmd_diagnose)
    args = ap.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
