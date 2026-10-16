"""AXIOM-MONEY-PRINTER — GUI update system (master spec section 69).

CHECK → DOWNLOAD → VERIFY → PREPARE → INSTALL with truthful statuses. The
network is replaced by an injectable transport; the stub transport below is
identified as a MOCK and is tested as a mock — real release checks go
through urllib in production (``default_transport``).
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

from app.system.updater import (
    CHECK_FAILED,
    DOWNLOAD_FAILED,
    DOWNLOADED,
    INSTALL_REFUSED,
    INSTALLED_RESTART_REQUIRED,
    PREPARE_REFUSED,
    PREPARED,
    UP_TO_DATE,
    UPDATE_AVAILABLE,
    VERIFIED,
    VERIFY_REFUSED,
    UpdateManager,
    _parse_version,
)


class MockTransport:
    """MOCK transport — deterministic, offline. Never used in production."""

    def __init__(self, responses: dict[str, bytes]):
        self.responses = responses
        self.calls: list[str] = []

    def __call__(self, url: str, *, timeout: float = 10.0) -> bytes:
        self.calls.append(url)
        if url not in self.responses:
            raise ConnectionError(f"mock transport has no route for {url}")
        return self.responses[url]


@pytest.fixture()
def manager(tmp_path):
    def make(transport=None, version="3.2.0", target=None):
        return UpdateManager(tmp_path / "updates", current_version=version,
                             transport=transport or MockTransport({}),
                             target_path=target)
    return make


class TestCheck:
    def test_update_available_is_reported(self, manager):
        transport = MockTransport({
            "https://api.github.com/repos/x/releases/latest":
                json.dumps({"tag_name": "v3.3.0", "name": "3.3.0",
                            "published_at": "2026-10-01T00:00:00Z"}).encode()})
        update = manager(transport)
        update.releases_url = "https://api.github.com/repos/x/releases/latest"
        out = update.check()
        assert out["ok"] is True
        assert out["status"] == UPDATE_AVAILABLE
        assert (out["current"], out["latest"]) == ("3.2.0", "3.3.0")
        assert out["update_available"] is True

    def test_up_to_date_when_tag_matches(self, manager):
        transport = MockTransport({
            "https://api.github.com/repos/x/releases/latest":
                json.dumps({"tag_name": "v3.2.0"}).encode()})
        update = manager(transport)
        update.releases_url = "https://api.github.com/repos/x/releases/latest"
        out = update.check()
        assert out["status"] == UP_TO_DATE and out["update_available"] is False

    def test_network_failure_is_check_failed_not_assumed(self, manager):
        out = manager(MockTransport({})).check()
        assert out["ok"] is False and out["status"] == CHECK_FAILED
        assert out["current"] == "3.2.0"

    def test_tag_parsing(self):
        assert _parse_version("v3.3.0") == "3.3.0"
        assert _parse_version("3.3.0") == "3.3.0"
        assert _parse_version("v3.3.0-rc.2") == "3.3.0-rc.2"
        assert _parse_version("release-odd") == "release-odd"

    def test_older_release_is_not_misreported_as_an_update(self, manager):
        transport = MockTransport({
            "https://api.github.com/repos/x/releases/latest":
                json.dumps({"tag_name": "v3.1.9"}).encode()})
        update = manager(transport)
        update.releases_url = "https://api.github.com/repos/x/releases/latest"
        out = update.check()
        assert out["status"] == UP_TO_DATE
        assert out["update_available"] is False
        assert "downgrades are disabled" in out["note"]

    def test_semver_prerelease_does_not_look_newer_than_release(self, manager):
        transport = MockTransport({
            "https://api.github.com/repos/x/releases/latest":
                json.dumps({"tag_name": "v3.2.0-rc.2"}).encode()})
        update = manager(transport)
        update.releases_url = "https://api.github.com/repos/x/releases/latest"
        out = update.check()
        assert out["status"] == UP_TO_DATE
        assert out["update_available"] is False


class TestDownloadVerifyPrepareInstall:
    def _staged(self, manager, payload=b"AXIOM-UPDATE-PAYLOAD"):
        digest = hashlib.sha256(payload).hexdigest()
        transport = MockTransport({"https://cdn.example/Arena": payload})
        update = manager(transport)
        downloaded = update.download("https://cdn.example/Arena", filename="Arena")
        assert downloaded["status"] == DOWNLOADED and downloaded["sha256"] == digest
        return update, downloaded, digest

    def test_download_unsafe_filename_refused(self, manager):
        update = manager(MockTransport({"u": b"x"}))
        out = update.download("u", filename="../evil")
        assert out["status"] == DOWNLOAD_FAILED
        assert not (update.staging_dir / "evil").exists()

    def test_verify_mismatch_refused(self, manager):
        update, downloaded, _ = self._staged(manager)
        out = update.verify(downloaded["path"], "0" * 64)
        assert out["ok"] is False and out["status"] == VERIFY_REFUSED

    def test_verify_path_confinement(self, manager):
        update, _, digest = self._staged(manager)
        outside = update.updates_dir.parent / "outside.bin"
        outside.write_bytes(b"whatever")
        out = update.verify(str(outside), digest)
        assert out["status"] == VERIFY_REFUSED

        sibling = update.updates_dir.parent / f"{update.updates_dir.name}-evil"
        sibling.mkdir()
        sibling_file = sibling / "outside.bin"
        sibling_file.write_bytes(b"whatever")
        assert update.verify(str(sibling_file), digest)["status"] == VERIFY_REFUSED

    def test_download_requires_https(self, manager):
        update = manager(MockTransport({"file:///etc/passwd": b"not used"}))
        result = update.download("file:///etc/passwd", filename="payload")
        assert result["status"] == DOWNLOAD_FAILED
        assert "HTTPS" in result["detail"]
        assert list(update.staging_dir.iterdir()) == []

    def test_full_pipeline_to_install(self, manager, tmp_path):
        target = tmp_path / "current-binary"
        target.write_bytes(b"OLD-BINARY")
        update, downloaded, digest = self._staged(manager)
        update.target_path = target
        verified = update.verify(downloaded["path"], digest)
        assert verified["status"] == VERIFIED
        assert verified["verification_basis"] == "caller-supplied-sha256-match"
        assert verified["release_authenticity_verified"] is False
        prepared = update.prepare(downloaded["path"])
        assert prepared["status"] == PREPARED
        assert prepared["verification_basis"] == "caller-supplied-sha256-match"
        assert prepared["release_authenticity_verified"] is False
        assert (update.updates_dir / "update-manifest.json").exists()
        # Install without preparation (separate updates dir) is refused…
        fresh = UpdateManager(tmp_path / "updates-other", current_version="3.2.0",
                              transport=MockTransport({}), target_path=target)
        assert fresh.install()["status"] == INSTALL_REFUSED
        # …but with the prepared manifest it replaces the binary + backs up.
        installed = update.install()
        assert installed["status"] == INSTALLED_RESTART_REQUIRED
        assert installed["verification_basis"] == "caller-supplied-sha256-match"
        assert installed["release_authenticity_verified"] is False
        assert "publisher authenticity/signature was not verified" in installed["detail"]
        assert target.read_bytes() == b"AXIOM-UPDATE-PAYLOAD"
        assert target.with_suffix(".previous").read_bytes() == b"OLD-BINARY"

    def test_rollback_backup_fsync_uses_a_writable_descriptor(
            self, manager, tmp_path, monkeypatch):
        """Both install temps must be fsync'd through a writable descriptor.

        This guards against a regression where one of the ``os.fsync`` calls
        is handed a read-only descriptor — on Linux that silently succeeds
        but on Windows it raises EBADF.
        """
        from app.system import updater as updater_module

        target = tmp_path / "current-binary"
        target.write_bytes(b"OLD-BINARY")
        update, downloaded, digest = self._staged(manager)
        update.target_path = target
        assert update.verify(downloaded["path"], digest)["status"] == VERIFIED
        assert update.prepare(downloaded["path"])["status"] == PREPARED
        real_open = open
        fsync_modes: dict[str, list[str]] = {"backup": [], "target": []}

        def tracking_open(path, mode="r", *args, **kwargs):
            candidate = Path(path)
            if (candidate.parent == target.parent
                    and candidate.name.startswith(".current-binary.previous.")
                    and candidate.name.endswith(".tmp")):
                fsync_modes["backup"].append(mode)
            elif (candidate.parent == target.parent
                    and candidate.name.startswith(".current-binary.")
                    and candidate.name.endswith(".install")):
                fsync_modes["target"].append(mode)
            return real_open(path, mode, *args, **kwargs)

        monkeypatch.setattr(updater_module, "open", tracking_open, raising=False)
        installed = update.install()
        assert installed["status"] == INSTALLED_RESTART_REQUIRED
        # The durable fsync must use a writable descriptor (Windows EBADF
        # regression). The later SHA-256 read (\"rb\") is unrelated.
        assert fsync_modes["backup"][0] == "r+b"
        assert fsync_modes["target"][0] == "r+b"

    def test_install_survives_transient_windows_share_lock(self, manager, tmp_path,
                                                           monkeypatch):
        """Windows Defender / Search Indexer briefly share-locks new files.

        ``os.replace`` used to fail with PermissionError (winerror 32 or 5)
        the moment after a ``shutil.copy2`` closes the destination handle.
        The updater must retry the rename briefly instead of returning
        INSTALL_REFUSED for a transient, non-safety failure.
        """
        import errno
        from app.system import updater as updater_module

        target = tmp_path / "current-binary"
        target.write_bytes(b"OLD-BINARY")
        update, downloaded, digest = self._staged(manager)
        update.target_path = target
        assert update.verify(downloaded["path"], digest)["status"] == VERIFIED
        assert update.prepare(downloaded["path"])["status"] == PREPARED

        real_replace = os.replace
        flake_budget = {"n": 3}

        class _WindowsShareLock(PermissionError):
            def __init__(self):
                super().__init__(errno.EACCES, "The process cannot access the file")
                self.winerror = 32

        def flaky_replace(source, dest):
            if flake_budget["n"] > 0:
                flake_budget["n"] -= 1
                raise _WindowsShareLock()
            return real_replace(source, dest)

        monkeypatch.setattr(updater_module.os, "replace", flaky_replace)
        installed = update.install()
        assert installed["status"] == INSTALLED_RESTART_REQUIRED
        assert target.read_bytes() == b"AXIOM-UPDATE-PAYLOAD"
        assert target.with_suffix(".previous").read_bytes() == b"OLD-BINARY"
        # All three retries were consumed so we know the retry fired.
        assert flake_budget["n"] == 0

    def test_permanent_permission_failure_still_refuses(self, manager, tmp_path,
                                                        monkeypatch):
        """A permanent permission error must NOT be retried into success."""
        from app.system import updater as updater_module

        target = tmp_path / "current-binary"
        target.write_bytes(b"OLD-BINARY")
        update, downloaded, digest = self._staged(manager)
        update.target_path = target
        assert update.verify(downloaded["path"], digest)["status"] == VERIFIED
        assert update.prepare(downloaded["path"])["status"] == PREPARED

        def refusing_replace(source, dest):
            # A permanent failure (winerror 87 = invalid parameter, or simply
            # no winerror and errno=EROFS) must NOT be retried.
            exc = OSError(errno.EROFS, "read-only file system")
            raise exc

        monkeypatch.setattr(updater_module.os, "replace", refusing_replace)
        installed = update.install()
        assert installed["status"] == INSTALL_REFUSED
        # Target was NOT replaced.
        assert target.read_bytes() == b"OLD-BINARY"

    def test_checksum_match_is_not_reported_as_publisher_authenticity(self, manager):
        update, downloaded, digest = self._staged(manager)
        assert downloaded["digest_role"] == "locally-computed-only"
        assert downloaded["release_authenticity_verified"] is False
        verified = update.verify(downloaded["path"], digest)
        assert verified["verification_basis"] == "caller-supplied-sha256-match"
        assert verified["release_authenticity_verified"] is False
        assert "not a signature" in verified["note"]
        assert update.status()["release_authenticity_verified"] is False
        assert "caller-supplied SHA-256" in update.status()["note"]

    def test_prepare_missing_artifact_refused(self, manager):
        update = manager()
        out = update.prepare(str(update.staging_dir / "ghost.bin"))
        assert out["status"] == PREPARE_REFUSED

    def test_prepare_requires_successful_verify(self, manager):
        update, downloaded, digest = self._staged(manager)
        refused = update.prepare(downloaded["path"])
        assert refused["status"] == PREPARE_REFUSED
        assert "VERIFY" in refused["detail"]
        assert not (update.updates_dir / "update-manifest.json").exists()

        assert update.verify(downloaded["path"], "0" * 64)["status"] == VERIFY_REFUSED
        assert update.prepare(downloaded["path"])["status"] == PREPARE_REFUSED
        assert update.verify(downloaded["path"], digest)["status"] == VERIFIED
        assert update.prepare(downloaded["path"])["status"] == PREPARED

    def test_source_mode_never_targets_sys_executable(self, manager, tmp_path):
        update, downloaded, digest = self._staged(manager)
        assert update.verify(downloaded["path"], digest)["status"] == VERIFIED
        assert update.prepare(downloaded["path"])["status"] == PREPARED
        # No frozen application target was supplied. The implementation must
        # refuse rather than falling back to the running Python interpreter.
        update.target_path = None
        before = Path(sys.executable).read_bytes()[:128]
        result = update.install()
        assert result["status"] == INSTALL_REFUSED
        assert "sys.executable" in result["detail"]
        assert Path(sys.executable).read_bytes()[:128] == before

    def test_install_detects_tampered_artifact(self, manager, tmp_path):
        target = tmp_path / "bin"
        target.write_bytes(b"OLD")
        update, downloaded, digest = self._staged(manager)
        update.target_path = target
        assert update.verify(downloaded["path"], digest)["status"] == VERIFIED
        assert update.prepare(downloaded["path"])["status"] == PREPARED
        (update.updates_dir / "Arena").write_bytes(b"TAMPERED")
        out = update.install()
        assert out["ok"] is False and out["status"] == INSTALL_REFUSED
        assert target.read_bytes() == b"OLD"

    def test_status_reports_staged_files_honestly(self, manager):
        update, _, _ = self._staged(manager)
        status = update.status()
        assert status["current"] == "3.2.0"
        assert "Arena" in status["staged"]
        assert status["manifest"] is None  # nothing prepared yet
