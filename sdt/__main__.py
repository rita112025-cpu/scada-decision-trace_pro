"""命令列：python -m sdt <指令>。"""
import argparse
import http.client
import pathlib
import sys
import webbrowser

from . import agenda, backup, config, csvio, db, sample, server


def _server_running(port):
    try:
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=1.5)
        c.request("GET", "/api/meta", headers={"Host": f"127.0.0.1:{port}"})
        return c.getresponse().status == 200
    except OSError:
        return False


def cmd_serve(cfg, args):
    port = args.port or cfg.port
    if _server_running(port):
        print(f"已有服務在執行：http://127.0.0.1:{port}/")
        return 0
    srv = server.make_server(cfg, port)
    url = f"http://127.0.0.1:{port}/"
    print(f"SCADA 決議追蹤器已啟動：{url}")
    print("停止方式：在此視窗按 Ctrl+C，或執行 stop.bat")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    print("已停止")
    return 0


def cmd_stop(cfg, args):
    port = args.port or cfg.port
    if not _server_running(port):
        print("服務未在執行")
        return 0
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    c.request("POST", "/api/shutdown", body=b"{}",
              headers={"Host": f"127.0.0.1:{port}", "X-SDT": "1", "Content-Type": "application/json"})
    print("已送出停止指令" if c.getresponse().status == 200 else "停止失敗，請在啟動視窗按 Ctrl+C")
    return 0


def cmd_backup(cfg, args):
    db.connect(cfg.db_path).close()
    print(f"已備份：{backup.backup(cfg.db_path, cfg.backup_dir)}")
    return 0


def cmd_restore(cfg, args):
    if _server_running(cfg.port):
        print("請先停止服務（stop.bat）再還原")
        return 1
    try:
        pre = backup.restore(cfg.db_path, args.file, cfg.backup_dir)
    except ValueError as e:
        print(f"還原失敗：{e}")
        return 1
    if pre:
        print(f"還原前已自動備份目前資料：{pre}")
    print("還原完成")
    return 0


def _confirm_twice(phrase):
    if input("確定要清除所有範例資料？(y/N) ").strip().lower() != "y":
        return False
    return input(f"請輸入「{phrase}」再次確認：").strip() == phrase


def cmd_clear_sample(cfg, args):
    if not _confirm_twice(server.CLEAR_SAMPLE_PHRASE):
        print("已取消")
        return 1
    pre = backup.backup(cfg.db_path, cfg.backup_dir, label="pre-clear-sample")
    with db.session(cfg.db_path) as conn:
        n = db.clear_sample(conn, cfg.operator)
    print(f"已清除 {n} 筆範例資料（清除前備份：{pre}）")
    return 0


def cmd_load_sample(cfg, args):
    with db.session(cfg.db_path) as conn:
        n = sample.load_sample(conn, cfg.operator)
    print(f"已載入 {n} 筆範例資料" if n else "範例資料已存在，未重複載入")
    return 0


def cmd_export(cfg, args):
    with db.session(cfg.db_path) as conn:
        pathlib.Path(args.file).write_bytes(csvio.export_issues(db.list_issues(conn, sort="issue_id")))
    print(f"已匯出：{args.file}")
    return 0


def cmd_import(cfg, args):
    pre = backup.backup(cfg.db_path, cfg.backup_dir, label="pre-import")
    try:
        with db.session(cfg.db_path) as conn:
            r = csvio.import_issues(conn, pathlib.Path(args.file).read_bytes(), cfg.operator)
    except db.ValidationError as e:
        print("匯入失敗，未寫入任何資料：")
        for err in e.errors:
            print(f"  - {err}")
        return 1
    print(f"新增 {r['created']}、更新 {r['updated']}、未變更 {r['unchanged']}（匯入前備份：{pre}）")
    for w in r["warnings"]:
        print(f"  ! {w}")
    return 0


def cmd_agenda(cfg, args):
    with db.session(cfg.db_path) as conn:
        text = agenda.build_agenda(conn)
    if args.out:
        pathlib.Path(args.out).write_text(text, encoding="utf-8-sig")
        print(f"已輸出：{args.out}")
    else:
        sys.stdout.write(text)
    return 0


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    p = argparse.ArgumentParser(prog="python -m sdt", description="SCADA 決議—模型—圖紙追蹤器")
    p.add_argument("--config", help="config.json 路徑")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="啟動本機網頁")
    s.add_argument("--port", type=int)
    s.add_argument("--no-browser", action="store_true")
    s = sub.add_parser("stop", help="停止服務")
    s.add_argument("--port", type=int)
    sub.add_parser("backup", help="備份資料庫")
    s = sub.add_parser("restore", help="從備份還原（會先自動備份目前資料）")
    s.add_argument("file")
    sub.add_parser("load-sample", help="載入虛構範例資料")
    sub.add_parser("clear-sample", help="清除範例資料（需二次確認）")
    s = sub.add_parser("export", help="匯出 CSV")
    s.add_argument("file")
    s = sub.add_parser("import", help="匯入 CSV")
    s.add_argument("file")
    s = sub.add_parser("agenda", help="產生議程草稿 Markdown")
    s.add_argument("--out")
    args = p.parse_args(argv)
    cfg = config.load(args.config)
    try:
        return _run(cfg, args)
    except ValueError as e:
        print(f"錯誤：{e}")
        return 1


def _run(cfg, args):
    handlers ={"serve": cmd_serve, "stop": cmd_stop, "backup": cmd_backup, "restore": cmd_restore,
                "load-sample": cmd_load_sample, "clear-sample": cmd_clear_sample,
                "export": cmd_export, "import": cmd_import, "agenda": cmd_agenda}
    return handlers[args.cmd](cfg, args)


if __name__ == "__main__":
    sys.exit(main())
