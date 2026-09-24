import gzip
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
FIXTURES = Path(__file__).parent / "fixtures"

import localdb  # noqa: E402


@pytest.fixture
def mini_dump(tmp_path):
    """The fixture dump gzipped, with its DTD beside it, as update-db leaves
    them. Deliberately not in the cwd, so DTD resolution cannot cheat."""
    xml_gz = tmp_path / "dblp-2000-01-01.xml.gz"
    with open(FIXTURES / "mini.xml", "rb") as src, gzip.open(xml_gz, "wb") as dst:
        shutil.copyfileobj(src, dst)
    shutil.copy(FIXTURES / "dblp-test.dtd", tmp_path)
    return xml_gz


@pytest.fixture
def mini_db(mini_dump, tmp_path, monkeypatch):
    monkeypatch.setenv("DBZ_DATA_DIR", str(tmp_path / "data"))
    localdb.close()
    localdb.build_from(mini_dump, "2000-01-01")
    yield localdb
    localdb.close()


@pytest.fixture
def no_db(tmp_path, monkeypatch):
    monkeypatch.setenv("DBZ_DATA_DIR", str(tmp_path / "empty"))
    localdb.close()
    yield
    localdb.close()
