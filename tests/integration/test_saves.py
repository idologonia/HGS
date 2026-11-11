"""Saves: manual snapshots, backups, restore (and its safety)."""

import json
import zipfile

from tests.integration.test_workers import add_start, logic_game


async def test_manual_save_backup_and_restore_snapshot(hgs, tmp_path):
    await add_start(hgs, logic_game(tmp_path, "keeper"), "keeper")
    c = await hgs.client("keeper", "Ann")
    await c.join()  # counter = 1
    first = (await hgs.admin.post("/games/keeper/save")).json()
    assert first["path"].endswith("_manual.json")
    c2 = await hgs.client("keeper", "Bob")
    await c2.join()  # counter = 2
    r = await hgs.admin.post("/games/keeper/restore-snapshot", json={"file": first["path"].split("/")[-1]})
    assert r.status_code == 409  # must be stopped
    await hgs.admin.post("/games/keeper/stop", json={"grace_s": 0})  # saves counter = 2
    saves = (await hgs.admin.get("/games/keeper/saves")).json()
    files = [s["file"] for s in saves["snapshots"]]
    assert files[0].endswith("_stop.json") and any(f.endswith("_manual.json") for f in files)
    manual = next(f for f in files if f.endswith("_manual.json"))
    r = await hgs.admin.post("/games/keeper/restore-snapshot", json={"file": manual})
    assert r.status_code == 200, r.text
    saves = (await hgs.admin.get("/games/keeper/saves")).json()
    assert saves["snapshots"][0]["file"].endswith("_restore.json")
    assert saves["backups"][0]["file"].endswith("_before_restore.zip")  # restore can be undone
    assert any(j["reason"] == "restore" for j in saves["journal"])
    await hgs.admin.post("/games/keeper/start")
    d = await hgs.client("keeper", "Cid")
    await d.join()
    await d.send("game", {"e": "echo"})
    while (m := await d.recv_type("event"))["d"]["e"] != "echo":
        pass
    assert m["d"]["d"]["counter"] == 2  # 1 restored from the manual snapshot + Cid


async def test_backup_restore_and_unsafe_archives(hgs, tmp_path):
    await add_start(hgs, logic_game(tmp_path, "bk"), "bk")
    await hgs.admin.post("/games/bk/save")
    b = (await hgs.admin.post("/games/bk/backup")).json()
    await hgs.admin.post("/games/bk/stop", json={"grace_s": 0})
    saves_dir = tmp_path / "data" / "games" / "bk" / "saves"
    for f in saves_dir.iterdir():
        f.unlink()
    r = await hgs.admin.post("/games/bk/restore-backup", json={"file": b["file"]})
    assert r.status_code == 200, r.text
    assert any(p.name.endswith("_manual.json") for p in saves_dir.iterdir())
    # path traversal in names and inside archives is refused
    assert (await hgs.admin.post("/games/bk/restore-backup", json={"file": "../../hgs.db"})).status_code == 400
    evil = tmp_path / "data" / "games" / "bk" / "backups" / "evil.zip"
    with zipfile.ZipFile(evil, "w") as z:
        z.writestr("../../escape.txt", "x")
    r = await hgs.admin.post("/games/bk/restore-backup", json={"file": "evil.zip"})
    assert r.status_code == 400 and "unsafe" in r.json()["message"]
    assert not (tmp_path / "data" / "escape.txt").exists()


async def test_relay_game_has_no_snapshots(hgs):
    await hgs.add_and_start("plainrelay")
    r = await hgs.admin.post("/games/plainrelay/save")
    assert r.status_code == 400 and "does not save" in r.json()["message"]
    assert json.loads((await hgs.admin.get("/games/plainrelay/saves")).text)["snapshots"] == []
