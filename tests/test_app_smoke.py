"""Render every page with Streamlit's AppTest against the real state file produced by a pipeline run."""
import os, shutil, pytest
from streamlit.testing.v1 import AppTest

PAGES = ["dashboard", "pipeline", "positions", "watchlist", "log", "reports", "settings", "rules"]

@pytest.fixture(scope="module", autouse=True)
def _state():
    src = os.path.join(os.path.dirname(__file__), "..", "data", "state.json")
    dst = "/tmp/wso_test_state.json"
    if os.path.exists(src):
        shutil.copy(src, dst)
    elif os.path.exists(dst):
        os.remove(dst)
    os.environ["WSO_STATE_PATH"] = dst
    yield

@pytest.mark.parametrize("page", PAGES)
def test_page_renders(page):
    os.environ["WSO_PAGE"] = page
    at = AppTest.from_file(os.path.join(os.path.dirname(__file__), "..", "app.py"), default_timeout=120).run()
    assert not at.exception, [e.value for e in at.exception]
    assert at.title and page.lower()[:4] in at.title[0].value.lower() or page in ("log", "positions", "pipeline")
