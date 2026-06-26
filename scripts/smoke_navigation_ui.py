import os
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

API_ROOT = ROOT / "apps" / "api"
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))


def assert_ok(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(message)


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="video_pipeline_nav_smoke_") as tmp:
        tmp_path = Path(tmp)
        os.environ["DATA_DIR"] = str(tmp_path / "data")
        os.environ["DATABASE_URL"] = f"sqlite:///{(tmp_path / 'smoke.db').as_posix()}"
        os.environ["CELERY_BROKER_URL"] = "memory://"
        os.environ["CELERY_RESULT_BACKEND"] = "cache+memory://"
        os.environ["SECRETS_FILE"] = str(tmp_path / "missing.env")
        os.environ["OPENROUTER_API_KEY"] = ""
        os.environ["GOOGLE_API_KEY"] = ""

        from fastapi.testclient import TestClient

        from app.main import app

        with TestClient(app) as client:
            home = client.get("/")
            assert_ok(home.status_code == 200, f"Home returned {home.status_code}")
            assert_ok('class="global-nav"' in home.text, "Global nav missing on home")
            assert_ok("Video Pipeline Dashboard" in home.text, "Dashboard title missing")
            assert_ok('href="/settings/lava"' in home.text, "LAVA settings nav missing")
            assert_ok('href="/docs"' in home.text, "API docs nav missing")

            lava = client.get("/settings/lava")
            assert_ok(lava.status_code == 200, f"LAVA settings returned {lava.status_code}")
            assert_ok('class="global-nav"' in lava.text, "Global nav missing on LAVA settings")
            assert_ok("Task Bindings" in lava.text, "LAVA task table missing")

            docs = client.get("/docs")
            assert_ok(docs.status_code == 200, f"API docs returned {docs.status_code}")

            create = client.post(
                "/",
                data={
                    "title": "Navigation Smoke Project",
                    "script_markdown": (
                        "<VISUAL_BREAK: workflow diagram>\n"
                        "This deterministic project checks UI navigation.\n"
                        "<BROLL: editing timeline>\n"
                        "The status flow should be visible."
                    ),
                    "language": "en",
                    "persona": "tester",
                },
                follow_redirects=False,
            )
            assert_ok(create.status_code == 303, f"Project create returned {create.status_code}")
            detail_url = create.headers["Location"]

            detail = client.get(detail_url)
            assert_ok(detail.status_code == 200, f"Project detail returned {detail.status_code}")
            assert_ok('class="global-nav"' in detail.text, "Global nav missing on project detail")
            assert_ok("Project Status Flow" in detail.text, "Status flow missing on project detail")
            assert_ok("Next Recommended Action" in detail.text, "Next action missing on project detail")
            assert_ok("Missing Prerequisites" in detail.text, "Missing prerequisite text absent")
            assert_ok("Run CPU ASR" in detail.text, "ASR action missing on project detail")
            assert_ok("Run LAVA Brain Workflow" in detail.text, "LAVA workflow action missing")
            assert_ok("Review Gate" in detail.text, "Human review gate missing")

    print("OK navigation UI smoke")


if __name__ == "__main__":
    main()
