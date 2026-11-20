import json

from scripts.poc.fakturownia_poc_evidence import Evidence


def test_run_folder_holds_jsonl_logs_and_a_summary(tmp_path):
    ev = Evidence.start(tmp_path, experiment="E1", report="jpk_fa")
    ev.request({"url": "https://piotrgryzlo.fakturownia.pl/a", "allowed": True})
    ev.click({"control": "Generuj raport", "url": "https://piotrgryzlo.fakturownia.pl/reports"})
    ev.finish(outcome="ok", files=["JPK_FA.xml"], extra={"aborted": 0})
    assert ev.dir.parent == tmp_path
    assert ev.dir.name.endswith("-E1-jpk_fa")
    assert [
        json.loads(line)["url"] for line in (ev.dir / "requests.jsonl").read_text().splitlines()
    ] == ["https://piotrgryzlo.fakturownia.pl/a"]
    assert json.loads((ev.dir / "clicks.jsonl").read_text())["control"] == "Generuj raport"
    run = json.loads((ev.dir / "run.json").read_text())
    assert run["experiment"] == "E1" and run["outcome"] == "ok" and run["files"] == ["JPK_FA.xml"]
    assert run["duration_s"] >= 0 and "peak_rss_mb" in run


def test_secrets_never_reach_the_evidence(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKTUROWNIA_PASSWORD", "s3cret-value")
    ev = Evidence.start(tmp_path, experiment="E1", report="jpk_fa")
    ev.request({"url": "https://piotrgryzlo.fakturownia.pl/login?p=s3cret-value"})
    ev.finish(outcome="error", error="login failed with s3cret-value")
    for path in ev.dir.iterdir():
        assert "s3cret-value" not in path.read_text(errors="ignore")
