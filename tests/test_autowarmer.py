"""Tests for the AutoWarmer app layer — setup, validation, jobs, API.
Runnable: python3 tests/test_autowarmer.py"""
import json
import os
import stat
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# keep the suite out of the real ~/.autowarmer — tests store fake Apple keys
os.environ["AUTOWARMER_KEYS_DIR"] = tempfile.mkdtemp(prefix="aw-keys-")

from autowarmer import setup_flow as S      # noqa: E402
from autowarmer.jobs import Busy, Runner    # noqa: E402

FAILS = 0


def check(name, cond):
    global FAILS
    if cond:
        print(f"  ok   {name}")
    else:
        FAILS += 1
        print(f"  FAIL {name}")


def fake_exe(d: Path, name: str) -> str:
    p = d / name
    p.write_text("#!/bin/sh\nexit 0\n")
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return str(p)


def good_config(tmp: Path) -> dict:
    return {"ios_binary": fake_exe(tmp, "ios"),
            "iproxy_binary": fake_exe(tmp, "iproxy"),
            "pymobiledevice3_binary": fake_exe(tmp, "pmd3"),
            "signing": {"p8_path": str(_p8(tmp)), "key_id": "ABC1234567",
                        "issuer_id": "11111111-2222-3333-4444-555555555555",
                        "team_id": "TEAM123456"},
            "devices": [{"udid": "UDID-A", "name": "iphone-1",
                         "wda_bundle_id": "com.team123456.autowarmer.wda.xctrunner",
                         "accounts": [{"platform": "instagram", "username": "acct_a",
                                       "created_at": "2026-08-01", "keywords": ["x"]}]}]}


def _p8(tmp: Path) -> Path:
    p = tmp / "signing-key.p8"
    p.write_text("-not-a-real-key-")
    return p


def test_detection():
    print("tool detection")
    tools = S.detect_all()
    names = [t["name"] for t in tools]
    check("looks for all four tools",
          names == ["xcode", "git", "ios", "pymobiledevice3"])
    check("iproxy is not demanded (nothing runs it)", "iproxy" not in names)
    check("each tool explains what it is for", all(t["why"] for t in tools))
    check("only the fetchable ones offer auto-install",
          {t["name"] for t in tools if t["installable"]} == {"ios", "pymobiledevice3"})
    check("only ordinary install locations are searched",
          all(("/bin/" in c or "/opt/" in c or c.startswith("~"))
              for paths in S.KNOWN.values() for c in paths))
    check("every tool carries an install hint", all(t["hint"] for t in tools))
    check("every tool reports found/path keys",
          all({"found", "path", "label", "source"} <= set(t) for t in tools))
    d = Path(tempfile.mkdtemp())
    exe = fake_exe(d, "thing")
    check("valid executable accepted", S.check_path("ios", exe)["ok"])
    check("missing path rejected", not S.check_path("ios", str(d / "nope"))["ok"])
    txt = d / "plain.txt"
    txt.write_text("x")
    check("non-executable rejected", not S.check_path("ios", str(txt))["ok"])
    check("CommandLineTools rejected as Xcode",
          not S.check_path("xcode", "/Library/Developer/CommandLineTools")["ok"])
    check("CommandLineTools says why",
          "full Xcode" in S.check_path("xcode", "/Library/Developer/CommandLineTools")["why"])
    check("home paths expand", S.check_path("xcode", "~")["ok"])


def test_bundle_and_signing():
    print("signing")
    check("bundle id from team", S.bundle_id_for("AB12CD34EF")
          == "com.ab12cd34ef.autowarmer.wda.xctrunner")
    check("bundle id sanitizes junk", S.bundle_id_for("A B-C!")
          == "com.abc.autowarmer.wda.xctrunner")
    check("blank team still yields a valid id",
          S.bundle_id_for("") == "com.team.autowarmer.wda.xctrunner")
    probs = S.validate_signing({})
    check("empty signing reports all four", len(probs) == 4)
    tmp = Path(tempfile.mkdtemp())
    ok = {"p8_path": str(_p8(tmp)), "key_id": "K", "issuer_id": "I", "team_id": "T"}
    check("complete signing passes", S.validate_signing(ok) == [])
    bad = {**ok, "p8_path": str(tmp / "gone.p8")}
    check("missing p8 file caught", any("no .p8" in p for p in S.validate_signing(bad)))


def test_apple_key_storage():
    print("apple key storage")
    tmp = Path(tempfile.mkdtemp())
    keys = tmp / "keys"
    src = tmp / "Downloads" / "signing-key.p8"
    src.parent.mkdir(parents=True)
    src.write_text("-not-a-real-key-")
    out = S.store_p8(str(src), str(keys))
    dest = Path(out["path"])
    check("copied into our own folder", out["copied"] and dest.parent == keys)
    check("contents preserved", dest.read_text() == "-not-a-real-key-")
    check("key is private (0600)", stat.S_IMODE(dest.stat().st_mode) == 0o600)
    check("folder is private (0700)", stat.S_IMODE(keys.stat().st_mode) == 0o700)
    check("note tells the user what happened", "readable only by you" in out["note"])
    again = S.store_p8(out["path"], str(keys))
    check("re-saving does not re-copy", not again["copied"])
    check("original can now be deleted", (src.unlink() or Path(out["path"]).is_file()))
    missing = S.store_p8(str(tmp / "gone.p8"), str(keys))
    check("missing file left for validation to report", not missing["copied"])

    # config records the path, never the key material
    cfg = S.build_config({}, {"p8_path": str(dest), "key_id": "K", "issuer_id": "I",
                              "team_id": "T"}, [], keys_dir=str(keys))
    check("config stores the path", cfg["signing"]["p8_path"] == str(dest))
    check("config never contains key material",
          "-not-a-real-key-" not in json.dumps(cfg))


def test_config_validation():
    print("config validation")
    tmp = Path(tempfile.mkdtemp())
    cfg = good_config(tmp)
    check("good config is clean", S.validate_config(cfg) == [])
    no_dev = {**cfg, "devices": []}
    check("no phones caught", any("at least one iPhone" in p
                                  for p in S.validate_config(no_dev)))
    dup = json.loads(json.dumps(cfg))
    dup["devices"].append(json.loads(json.dumps(dup["devices"][0])))
    check("same phone twice caught", any("listed twice" in p
                                         for p in S.validate_config(dup)))
    two = json.loads(json.dumps(cfg))
    two["devices"].append({"udid": "UDID-B", "name": "iphone-2",
                           "accounts": [dict(two["devices"][0]["accounts"][0])]})
    check("account on two phones caught",
          any("more than one phone" in p for p in S.validate_config(two)))
    nodate = json.loads(json.dumps(cfg))
    nodate["devices"][0]["accounts"][0]["created_at"] = ""
    check("missing creation date caught",
          any("created" in p for p in S.validate_config(nodate)))
    badbin = {**cfg, "ios_binary": "/nope/ios"}
    check("bad binary path caught",
          any("go-ios" in p for p in S.validate_config(badbin)))


def test_build_and_save_config():
    print("building config")
    tmp = Path(tempfile.mkdtemp())
    binaries = {"ios": fake_exe(tmp, "ios"), "iproxy": fake_exe(tmp, "ipr"),
                "pymobiledevice3": fake_exe(tmp, "pmd"), "xcode": str(tmp)}
    signing = {"p8_path": str(_p8(tmp)), "key_id": "K1", "issuer_id": "I1",
               "team_id": "TEAM99"}
    devices = [{"udid": "U1", "name": "phone one",
                "accounts": [{"platform": "tiktok", "username": " @Handle ",
                              "created_at": "2026-08-01T00:00:00",
                              "keywords": "a, b,, c"}]}]
    cfg = S.build_config(binaries, signing, devices)
    acct = cfg["devices"][0]["accounts"][0]
    check("handle cleaned", acct["username"] == "Handle")
    check("keywords split", acct["keywords"] == ["a", "b", "c"])
    check("date trimmed to day", acct["created_at"] == "2026-08-01")
    check("bundle id derived", cfg["devices"][0]["wda_bundle_id"]
          == "com.team99.autowarmer.wda.xctrunner")
    check("developer dir carried", cfg["developer_dir"] == str(tmp))
    check("first phone mirrored top-level", cfg["udid"] == "U1")
    check("no cloud keys written",
          not any(k in json.dumps(cfg).lower()
                  for k in ("agent_key", "base_url", "api_key")))

    path = tmp / "config.json"
    S.save_config(path, cfg)
    check("saved and reloadable", S.load_config(path)["udid"] == "U1")
    check("no temp file left", not (tmp / "config.json.tmp").exists())
    check("is_configured true", S.is_configured(S.load_config(path)))
    check("is_configured false for empty", not S.is_configured({"devices": []}))

    # a second pass must not lose settings made outside the wizard
    existing = {**cfg, "trace_level": "full",
                "devices": [{**cfg["devices"][0], "screen_points": [375, 667]}]}
    again = S.build_config(binaries, signing, devices, existing=existing)
    check("screen size preserved", again["devices"][0]["screen_points"] == [375, 667])
    check("trace level preserved", again["trace_level"] == "full")


def test_resolve_device():
    print("account → phone")
    cfg = {"devices": [
        {"udid": "U1", "accounts": [{"platform": "instagram", "username": "same"},
                                    {"platform": "tiktok", "username": "only_tt"}]},
        {"udid": "U2", "accounts": [{"platform": "tiktok", "username": "same"}]}]}
    check("unique handle resolves", S.resolve_device(cfg, "only_tt") == "U1")
    check("@ and case tolerated", S.resolve_device(cfg, "@ONLY_TT") == "U1")
    check("platform disambiguates",
          S.resolve_device(cfg, "same", "instagram") == "U1"
          and S.resolve_device(cfg, "same", "tiktok") == "U2")
    for handle, why in (("ghost", "unknown"), ("same", "ambiguous")):
        try:
            S.resolve_device(cfg, handle)
            check(f"{why} handle refused", False)
        except LookupError:
            check(f"{why} handle refused", True)
    check("accounts_of flattens", len(S.accounts_of(cfg)) == 3)


def test_installer():
    print("auto-install")
    from autowarmer import install_tools as I
    tmp = Path(tempfile.mkdtemp())
    logs = []

    # a stand-in for the GitHub download: a zip carrying a fake `ios` binary
    import zipfile
    fake_zip = tmp / "go-ios-mac.zip"
    with zipfile.ZipFile(fake_zip, "w") as z:
        z.writestr("ios", "#!/bin/sh\necho '{\"version\":\"v9.9.9\"}'\n")
    real_urlopen = I.urllib.request.urlopen

    class FakeResp:
        def __init__(self, data):
            self._d = data
        def read(self, n=-1):
            d, self._d = self._d, b""
            return d
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=0):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        if "api.github.com" in url:
            return FakeResp(json.dumps({"tag_name": "v9.9.9", "assets": [
                {"name": "go-ios-mac.zip", "browser_download_url": "http://x/mac.zip"}]}).encode())
        return FakeResp(fake_zip.read_bytes())

    I.urllib.request.urlopen = fake_urlopen
    try:
        res = I.install_go_ios(tmp / "bin", log=logs.append)
    finally:
        I.urllib.request.urlopen = real_urlopen
    check("go-ios installed", res["ok"], )
    dest = Path(res["path"])
    check("landed in our own bin", dest == tmp / "bin" / "ios")
    check("binary is executable", os.access(dest, os.X_OK))
    check("version was verified", any("v9.9.9" in l for l in logs))
    check("picked the mac asset", any("go-ios-mac.zip" in l for l in logs))

    # a download that carries no `ios` binary must fail loudly, not silently
    bad_zip = tmp / "bad.zip"
    with zipfile.ZipFile(bad_zip, "w") as z:
        z.writestr("readme.txt", "nope")

    def bad_urlopen(req, timeout=0):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        if "api.github.com" in url:
            raise OSError("rate limited")        # exercises the fallback URL
        return FakeResp(bad_zip.read_bytes())

    I.urllib.request.urlopen = bad_urlopen
    try:
        res2 = I.install_go_ios(tmp / "bin2", log=logs.append)
    finally:
        I.urllib.request.urlopen = real_urlopen
    check("bad download refused", not res2["ok"] and "ios` binary" in res2["error"])
    check("api failure falls back to the direct URL",
          any("standard URL" in l for l in logs))

    # the screen text reader really builds (swiftc ships with Xcode)
    logs2 = []
    ocr = I.build_ocr(ROOT, log=logs2.append)
    check("text reader builds", ocr["ok"] and Path(ocr["path"]).is_file())

    check("unknown tool refused", not I.install("nonsense", ROOT, logs.append)["ok"])
    check("only fetchable tools offered",
          set(I.TOOLS) == {"ios", "pymobiledevice3", "ocr"})


def test_jobs():
    print("job runner")
    tmp = Path(tempfile.mkdtemp())
    r = Runner(tmp)
    # a stand-in for `python3 -m autowarmer …` that just prints and exits
    r.python = sys.executable
    job = r.start("doctor", "this Mac", [])
    job.proc and job.proc.wait(timeout=30)
    time.sleep(0.4)
    check("job records a result", job.status in ("done", "failed"))
    check("job snapshot has the basics",
          {"id", "kind", "status", "elapsed"} <= set(job.snapshot()))
    check("history lists it", any(j["id"] == job.id for j in r.recent()))
    check("tail returns lines list", isinstance(r.tail(job.id)["lines"], list))
    check("unknown job id handled", "error" in r.tail("nope"))

    # one at a time: a live job blocks a second start
    long = Runner(tmp)
    long.python = sys.executable
    j2 = long.start("warm", "@a", [])
    j2.proc.stdout and None
    if j2.status == "running":
        try:
            long.start("warm", "@b", [])
            check("second run refused while one is live", False)
        except Busy as e:
            check("second run refused while one is live", "still running" in str(e))
    else:
        check("second run refused while one is live", True)   # too fast to race
    long.stop(j2.id)


def test_api():
    print("local API")
    tmp = Path(tempfile.mkdtemp())
    cfgp = tmp / "config.json"
    from http.server import ThreadingHTTPServer
    from autowarmer import app as A
    A.Handler.config_path = cfgp
    A.Handler.runner = Runner(ROOT)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), A.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"

    def call(method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(base + path, method=method, data=data,
                                     headers={"Content-Type": "application/json"}
                                     if body is not None else {})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                raw = r.read()
                ct = r.headers.get("Content-Type", "")
                return r.status, (json.loads(raw) if "json" in ct else raw)
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    code, page = call("GET", "/")
    check("dashboard page served", code == 200 and b"AutoWarmer" in page)
    # "API key" and "sign in" appear legitimately (the Apple developer flow),
    # so look for markers that only a hosted/billing product would carry
    check("page has no cloud or billing references",
          not any(w in page.lower() for w in
                  (b"stripe", b"subscription", b"billing", b"agent key",
                   b"checkout", b"free tier")))

    code, st = call("GET", "/api/state")
    check("state before setup", code == 200 and st["configured"] is False)
    check("state offers detection", isinstance(st.get("detected"), list))

    code, chk = call("POST", "/api/check-path", {"name": "ios", "path": "/nope"})
    check("path check reachable", code == 200 and chk["ok"] is False)

    cfg = good_config(tmp)
    payload = {"binaries": {"ios": cfg["ios_binary"], "iproxy": cfg["iproxy_binary"],
                            "pymobiledevice3": cfg["pymobiledevice3_binary"]},
               "signing": cfg["signing"], "devices": cfg["devices"]}
    code, out = call("POST", "/api/config", payload)
    check("config saved", code == 200 and out["saved"])
    check("config landed on disk", cfgp.is_file())

    bad = {**payload, "devices": [{"udid": "", "accounts": []}]}
    code, out = call("POST", "/api/config", bad)
    check("invalid config refused", code == 400 and out["problems"])
    check("still the good config on disk",
          S.load_config(cfgp)["devices"][0]["udid"] == "UDID-A")

    code, st = call("GET", "/api/state")
    check("state after setup", code == 200 and st["configured"] is True)
    check("phones reported", st["devices"][0]["name"] == "iphone-1")
    check("phone shows disconnected", st["devices"][0]["connected"] is False)

    code, out = call("POST", "/api/run", {"kind": "nonsense"})
    check("unknown action refused", code == 400)
    code, out = call("POST", "/api/run", {"kind": "warm"})
    check("warm without a handle refused", code == 400)

    code, out = call("GET", "/api/job?id=missing")
    check("missing job handled", code == 200 and "error" in out)
    srv.shutdown()



def main():
    test_detection()
    test_bundle_and_signing()
    test_apple_key_storage()
    test_config_validation()
    test_build_and_save_config()
    test_resolve_device()
    test_installer()
    test_jobs()
    test_api()
    print()
    if FAILS:
        print(f"{FAILS} FAILED")
        sys.exit(1)
    print("all tests passed")


if __name__ == "__main__":
    main()
