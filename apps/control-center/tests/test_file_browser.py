import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import create_app
from file_browser_extension import init_file_browser
from services.file_browser_service import (
    FileNotAllowedError,
    FileTooLargeError,
    downloadable_file,
    list_directory,
    resolve_path,
)


def test_listing_hides_sensitive_and_hidden_files(tmp_path):
    root = tmp_path / "files"
    root.mkdir()
    (root / "folder").mkdir()
    (root / "normal.txt").write_text("hello")
    (root / ".env").write_text("SECRET=value")
    (root / ".hidden").write_text("hidden")
    (root / "private.pem").write_text("private")

    snapshot = list_directory(root)
    names = [entry["name"] for entry in snapshot["entries"]]
    assert names == ["folder", "normal.txt"]
    assert snapshot["read_only"] is True


def test_path_traversal_and_sensitive_files_are_blocked(tmp_path):
    root = tmp_path / "files"
    root.mkdir()
    (root / ".env").write_text("SECRET=value")

    with pytest.raises(FileNotAllowedError):
        resolve_path(root, "../outside")
    with pytest.raises(FileNotAllowedError):
        resolve_path(root, ".env")


def test_external_and_internal_symlinks_are_blocked(tmp_path):
    root = tmp_path / "files"
    root.mkdir()
    target = root / "target.txt"
    target.write_text("target")
    internal = root / "internal-link"
    internal.symlink_to(target)

    outside = tmp_path / "outside.txt"
    outside.write_text("outside")
    external = root / "external-link"
    external.symlink_to(outside)

    with pytest.raises(FileNotAllowedError):
        resolve_path(root, "internal-link")
    with pytest.raises(FileNotAllowedError):
        resolve_path(root, "external-link")


def test_download_size_limit(tmp_path):
    root = tmp_path / "files"
    root.mkdir()
    file_path = root / "large.bin"
    file_path.write_bytes(b"x" * 20)

    with pytest.raises(FileTooLargeError):
        downloadable_file(root, "large.bin", max_bytes=10)
    assert downloadable_file(root, "large.bin", max_bytes=20) == file_path


def test_api_dashboard_status_and_download(tmp_path):
    root = tmp_path / "files"
    root.mkdir()
    (root / "report.txt").write_text("safe content")

    app = create_app(
        {
            "TESTING": True,
            "DATA_ROOT": tmp_path / "data",
            "DATABASE_PATH": tmp_path / "data" / "test.db",
            "FILE_BROWSER_ROOTS": (root,),
            "FILE_BROWSER_MAX_DOWNLOAD_BYTES": 1024,
        }
    )
    init_file_browser(app)
    client = app.test_client()

    roots = client.get("/api/files")
    assert roots.status_code == 200
    assert roots.get_json()["count"] == 1

    listing = client.get("/api/files/0")
    assert listing.status_code == 200
    assert listing.get_json()["entries"][0]["name"] == "report.txt"

    download = client.get("/api/files/0/download?path=report.txt")
    assert download.status_code == 200
    assert download.data == b"safe content"
    assert "attachment" in download.headers["Content-Disposition"]

    status = client.get("/api/status")
    assert status.status_code == 200
    assert status.get_json()["file_browser"] == {
        "read_only": True,
        "roots": 1,
        "max_download_bytes": 1024,
    }

    dashboard = client.get("/")
    assert dashboard.status_code == 200
    html = dashboard.get_data(as_text=True)
    assert "File Browser" in html
    assert "Upload" in html
    assert "Blokeret" in html


def test_api_rejects_traversal(tmp_path):
    root = tmp_path / "files"
    root.mkdir()
    app = create_app(
        {
            "TESTING": True,
            "DATA_ROOT": tmp_path / "data",
            "DATABASE_PATH": tmp_path / "data" / "test.db",
            "FILE_BROWSER_ROOTS": (root,),
        }
    )
    init_file_browser(app)
    response = app.test_client().get("/api/files/0?path=../outside")
    assert response.status_code == 400
