"""Real subprocess + HTTP + filesystem scenarios; only synthetic documents."""
import json
import os
import shutil
import signal
from concurrent.futures import ThreadPoolExecutor
import sqlite3
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
from urllib.error import HTTPError
from urllib.request import Request, urlopen

REPO = Path(__file__).resolve().parents[1]


class DownloadFlow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="download-suggest-e2e-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.downloads = self.root / "Downloads"
        destination_root = self.root / "Documents"
        if os.environ.get("DOWNLOAD_SUGGEST_TEST_DESTINATION_ROOT"):
            destination_tmp = tempfile.TemporaryDirectory(dir=os.environ["DOWNLOAD_SUGGEST_TEST_DESTINATION_ROOT"])
            self.addCleanup(destination_tmp.cleanup)
            destination_root = Path(destination_tmp.name).resolve()
        self.destination = destination_root / "Housing"
        self.downloads.mkdir()
        self.destination.mkdir(parents=True)
        self.state_dir = self.root / "state"
        self.config = self.root / "config.json"
        self.config.write_text(json.dumps({
            "version": 1, "watch_dir": str(self.downloads),
            "folders": [{"id": "housing", "label": "Housing", "path": str(self.destination),
                         "description": "Housing invoices and rental documents"}],
            "rules": [{"keywords": ["invoice"], "folder_id": "housing"}],
            "model": {"enabled": False, "engine": "tinyjev", "name": "TinyJev-0.6B", "timeout_ms": 850},
            "poll_interval_ms": 100, "settle_ms": 300, "suggestion_deadline_ms": 1500,
            "instructions": "Only choose an existing destination."
        }))
        self.process = None
        self.addCleanup(self.stop)
        self.start()

    def start(self):
        env = dict(os.environ, PYTHONPATH=str(REPO / "src"), HOME=str(self.root))
        if os.environ.get("DOWNLOAD_SUGGEST_TEST_MODEL") == "1":
            env.setdefault("HF_HOME", str(Path.home() / ".cache" / "huggingface"))
        self.log = open(self.root / "server.log", "a+")
        self.process = subprocess.Popen([
            sys.executable, "-m", "download_suggest", "serve", "--config", str(self.config),
            "--state-dir", str(self.state_dir), "--port", "0"
        ], cwd=REPO, env=env, stdout=self.log, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                self.log.seek(0)
                self.fail("Backend exited: " + self.log.read())
            try:
                self.session = json.loads((self.state_dir / "session.json").read_text())
                if self.session["pid"] == self.process.pid:
                    status, _ = self.request("GET", "/v1/state")
                    if status == 200:
                        return
            except (OSError, ValueError, KeyError):
                pass
            time.sleep(.03)
        self.fail("Backend never became ready")

    def stop(self):
        if self.process is not None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
            self.process = None
            self.log.close()

    def request(self, method, path, body=None, auth=True, extra_headers=None, timeout=3):
        headers = {"Content-Type": "application/json"}
        if auth:
            headers["Authorization"] = "Bearer " + self.session["token"]
        headers.update(extra_headers or {})
        request = Request(f"http://127.0.0.1:{self.session['port']}{path}",
                          data=None if body is None else json.dumps(body).encode(),
                          method=method, headers=headers)
        try:
            response = urlopen(request, timeout=timeout)
        except HTTPError as error:
            response = error
        with response:
            raw = response.read()
            return response.status, json.loads(raw) if raw else {}

    def state(self):
        status, state = self.request("GET", "/v1/state")
        self.assertEqual(status, 200)
        return state

    def arrive(self, name="invoice.txt", contents="Invoice for example rental\n"):
        path = self.downloads / name
        path.write_text(contents)
        return path

    def pending(self, name="invoice.txt", timeout=4):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for item in self.state()["pending"]:
                if item["source_name"] == name:
                    return item
            time.sleep(.03)
        self.fail(f"No suggestion appeared for synthetic file {name}")

    def action(self, item, action, body=None):
        return self.request("POST", f"/v1/items/{item['id']}/{action}", body or {})

    def test_finished_download_suggests_under_two_seconds_without_moving(self):
        partial = self.arrive("invoice.txt.crdownload")
        time.sleep(.5)
        self.assertEqual(self.state()["pending"], [])
        started = time.monotonic()
        final = partial.with_suffix("")
        partial.rename(final)
        item = self.pending()
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(item["folder_id"], "housing")
        self.assertTrue(final.exists())
        self.assertEqual(list(self.destination.iterdir()), [])

    def test_accept_edited_name_and_undo_restore_exact_bytes(self):
        source = self.arrive()
        original = source.read_bytes()
        item = self.pending()
        status, _ = self.action(item, "apply", {"folder_id": "housing", "name": "Rental invoice.txt"})
        self.assertLess(status, 300)
        self.assertIn("decided_at", self.state()["recent"][0])
        target = self.destination / "Rental invoice.txt"
        self.assertEqual(target.read_bytes(), original)
        self.assertFalse(source.exists())
        status, _ = self.action(item, "undo")
        self.assertLess(status, 300)
        self.assertEqual(source.read_bytes(), original)
        self.assertFalse(target.exists())

    def test_collision_never_overwrites_either_file(self):
        source = self.arrive()
        item = self.pending()
        target = self.destination / "invoice.txt"
        target.write_text("Keep this existing invoice")
        status, _ = self.action(item, "apply", {"folder_id": "housing", "name": target.name})
        self.assertGreaterEqual(status, 400)
        self.assertTrue(source.exists())
        self.assertEqual(target.read_text(), "Keep this existing invoice")

    def test_changed_source_cannot_apply_stale_suggestion(self):
        source = self.arrive()
        item = self.pending()
        source.write_text("Different invoice, edited after suggestion")
        status, _ = self.action(item, "apply", {"folder_id": "housing", "name": "invoice.txt"})
        self.assertGreaterEqual(status, 400)
        self.assertTrue(source.exists())

    def test_powerpoint_without_metadata_uses_slide_text_to_choose_folder(self):
        path = self.downloads / 'figures.pptx'
        with zipfile.ZipFile(path, 'w') as archive:
            archive.writestr('ppt/slides/slide1.xml',
                '<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
                'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
                '<p:cSld><a:p><a:r><a:t>Rental in</a:t></a:r>'
                '<a:r><a:t>voice</a:t></a:r></a:p></p:cSld></p:sld>')
        item = self.pending('figures.pptx')
        self.assertEqual(item['folder_id'], 'housing')
        self.assertEqual(item['method'], 'rule')
        self.assertTrue(path.exists())

    def test_deleted_download_leaves_review_queue_without_a_move(self):
        source = self.arrive()
        item = self.pending()
        source.unlink()
        state = self.state()
        self.assertEqual(state['pending'], [])
        self.assertEqual(state['recent'][0]['status'], 'unavailable')
        status, _ = self.action(item, 'apply', {'folder_id': 'housing', 'name': 'invoice.txt'})
        self.assertEqual(status, 409)
        self.assertEqual(list(self.destination.iterdir()), [])

    def test_invalid_names_and_unknown_destination_leave_source_untouched(self):
        source = self.arrive()
        item = self.pending()
        for name in ("../escape.txt", "/tmp/escape.txt", "invoice.exe", "", "nested/invoice.txt"):
            with self.subTest(name=name):
                status, _ = self.action(item, "apply", {"folder_id": "housing", "name": name})
                self.assertGreaterEqual(status, 400)
        status, _ = self.action(item, "apply", {"folder_id": "unknown", "name": "invoice.txt"})
        self.assertGreaterEqual(status, 400)
        self.assertTrue(source.exists())
        self.assertEqual(list(self.destination.iterdir()), [])

    @unittest.skipUnless(sys.platform == "darwin" and os.environ.get("DOWNLOAD_SUGGEST_TEST_DESTINATION_ROOT"),
                         "Separate test volume not supplied")
    def test_state_remains_responsive_during_cross_volume_move(self):
        partial = self.downloads / "large-invoice.dat.part"
        with partial.open("wb") as stream:
            block = b"synthetic file data\n" * 65536
            for _ in range(200):
                stream.write(block)
        source = partial.with_suffix("")
        partial.rename(source)
        item = self.pending(source.name)
        self.assertNotEqual(source.stat().st_dev, self.destination.stat().st_dev)
        with ThreadPoolExecutor(max_workers=1) as pool:
            move = pool.submit(self.request, "POST", f"/v1/items/{item['id']}/apply",
                               {"folder_id": "housing", "name": source.name}, timeout=30)
            paused = None
            try:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and not move.done():
                    children = subprocess.run(["pgrep", "-P", str(self.process.pid), "-x", "ditto"],
                                              capture_output=True, text=True)
                    if children.stdout.strip():
                        paused = int(children.stdout.split()[0])
                        os.kill(paused, signal.SIGSTOP)
                        break
                    time.sleep(.005)
                self.assertIsNotNone(paused, "Copy process finished before the concurrency check")
                for _ in range(3):
                    started = time.monotonic()
                    state = self.state()
                    self.assertLess(time.monotonic() - started, .5)
                    self.assertIn(item['id'], [entry['id'] for entry in state['pending']])
            finally:
                if paused is not None:
                    try:
                        os.kill(paused, signal.SIGCONT)
                    except ProcessLookupError:
                        pass
            self.assertEqual(move.result(timeout=30)[0], 200)
        self.assertFalse(source.exists())
        self.assertTrue((self.destination / source.name).exists())
        self.assertEqual(self.action(item, "undo")[0], 200)
        self.assertTrue(source.exists())

    def test_leave_survives_restart_without_reprompting(self):
        source = self.arrive()
        item = self.pending()
        status, _ = self.action(item, "leave")
        self.assertLess(status, 300)
        self.stop()
        self.start()
        time.sleep(.6)
        self.assertEqual(self.state()["pending"], [])
        self.assertTrue(source.exists())

    def test_pending_survives_restart_once(self):
        self.arrive()
        item = self.pending()
        self.stop()
        self.start()
        time.sleep(.6)
        pending = self.state()["pending"]
        self.assertEqual([entry["id"] for entry in pending], [item["id"]])

    def test_loopback_api_requires_token_and_rejects_browser_origins(self):
        status, _ = self.request("GET", "/v1/state", auth=False)
        self.assertIn(status, (401, 403))
        status, _ = self.request("GET", "/v1/state", extra_headers={"Authorization": "Bearer wrong"})
        self.assertIn(status, (401, 403))
        status, _ = self.request("POST", "/v1/pause", {"paused": True},
                                 extra_headers={"Origin": "https://example.com"})
        self.assertIn(status, (401, 403))
        self.assertTrue(self.state()["watching"])

    def test_symlinks_and_directories_are_ignored(self):
        outside = self.root / "outside.txt"
        outside.write_text("Invoice outside Downloads")
        (self.downloads / "invoice.txt").symlink_to(outside)
        (self.downloads / "invoice-folder").mkdir()
        time.sleep(.7)
        self.assertEqual(self.state()["pending"], [])
        self.assertEqual(outside.read_text(), "Invoice outside Downloads")

    def test_pause_and_resume(self):
        status, _ = self.request("POST", "/v1/pause", {"paused": True})
        self.assertLess(status, 300)
        self.arrive()
        time.sleep(.6)
        self.assertEqual(self.state()["pending"], [])
        self.assertFalse(self.state()["watching"])
        status, _ = self.request("POST", "/v1/pause", {"paused": False})
        self.assertLess(status, 300)
        self.pending()

    def test_explicit_scan_reviews_existing_files(self):
        self.stop()
        shutil.rmtree(self.state_dir)  # Genuine first launch with pre-existing downloads.
        self.arrive()
        self.start()
        time.sleep(.5)
        self.assertEqual(self.state()["pending"], [])
        status, _ = self.request("POST", "/v1/scan", {})
        self.assertLess(status, 300)
        self.pending()

    def test_undo_collision_and_changed_destination_preserve_files(self):
        source = self.arrive()
        item = self.pending()
        status, _ = self.action(item, "apply", {"folder_id": "housing", "name": "invoice.txt"})
        self.assertLess(status, 300)
        target = self.destination / "invoice.txt"
        source.write_text("New download in the original location")
        status, _ = self.action(item, "undo")
        self.assertGreaterEqual(status, 400)
        self.assertEqual(source.read_text(), "New download in the original location")
        source.unlink()
        target.write_text("User changed the filed invoice")
        status, _ = self.action(item, "undo")
        self.assertGreaterEqual(status, 400)
        self.assertEqual(target.read_text(), "User changed the filed invoice")

    def test_interrupted_move_restart_keeps_both_files(self):
        source = self.arrive()
        item = self.pending()
        self.stop()
        target = self.destination / source.name
        shutil.copy2(source, target)
        with sqlite3.connect(self.state_dir / "state.sqlite3") as database:
            stored = json.loads(database.execute("SELECT data FROM items WHERE id=?", (item["id"],)).fetchone()[0])
            job = {"src": str(source), "dst": str(target), "source_fp": stored["source_fp"],
                   "initial_status": "pending", "final_status": "applied"}
            database.execute("INSERT INTO journal VALUES (?,?)", (item["id"], json.dumps(job)))
            # Recovery controls must survive a full recent-history page.
            for index in range(105):
                old = {**stored, "id": f"finished-{index}", "status": "left",
                       "created_at": stored["created_at"] + index + 1}
                database.execute("INSERT INTO items VALUES (?,?)", (old["id"], json.dumps(old)))
        self.start()
        recovered = next(entry for entry in self.state()["recent"] if entry["id"] == item["id"])
        self.assertEqual(recovered["status"], "interrupted")
        self.assertEqual(source.read_bytes(), target.read_bytes())

    def test_destination_symlink_swap_cannot_escape_allowed_folder(self):
        source = self.arrive()
        item = self.pending()
        outside = self.root / "Unconfigured"
        outside.mkdir()
        self.destination.rmdir()
        self.destination.symlink_to(outside, target_is_directory=True)
        status, _ = self.action(item, "apply", {"folder_id": "housing", "name": "invoice.txt"})
        self.assertGreaterEqual(status, 400)
        self.assertTrue(source.exists())
        self.assertEqual(list(outside.iterdir()), [])

    def test_private_decision_log_records_edits_undo_leave_and_survives_restart(self):
        self.stop()
        config = json.loads(self.config.read_text())
        alternate = self.root / "Documents" / "Records"
        alternate.mkdir(parents=True)
        config["folders"].append({"id": "records", "label": "Records", "path": str(alternate),
                                   "description": "Documents retained as records"})
        self.config.write_text(json.dumps(config))
        self.start()
        content_marker = "SYNTHETIC_CONTENT_MUST_NEVER_APPEAR_IN_AUDIT_83071"
        self.arrive(contents="Invoice for example rental\n" + content_marker)
        item = self.pending()
        status, _ = self.action(item, "apply", {"folder_id": "records", "name": "Edited rental.txt"})
        self.assertLess(status, 300)
        status, _ = self.action(item, "undo")
        self.assertLess(status, 300)
        self.arrive("second-invoice.txt")
        left = self.pending("second-invoice.txt")
        status, _ = self.action(left, "leave")
        self.assertLess(status, 300)
        audit_path = self.state_dir / "recommendations.jsonl"
        before_restart = audit_path.read_bytes()
        self.assertNotIn(content_marker.encode(), before_restart)
        self.assertEqual(audit_path.stat().st_mode & 0o777, 0o600)
        self.assertFalse(audit_path.is_relative_to(REPO))
        events = [json.loads(line) for line in before_restart.splitlines()]
        recommended = next(event for event in events if event["event"] == "recommendation" and event["item_id"] == item["id"])
        self.assertEqual(recommended["recommendation"]["folder_id"], "housing")
        self.assertEqual(recommended["recommendation"]["suggested_name"], item["suggested_name"])
        self.assertEqual(recommended["context"]["instructions"], config["instructions"])
        applied = next(event for event in events if event["event"] == "decision_result" and event["decision"]["action"] == "apply")
        self.assertEqual(applied["outcome"], "success")
        self.assertEqual(applied["decision"]["requested_name"], "Edited rental.txt")
        self.assertEqual(applied["decision"]["requested_folder_id"], "records")
        self.assertTrue(applied["decision"]["name_changed"])
        self.assertTrue(applied["decision"]["folder_changed"])
        attempts = {event["attempt_id"] for event in events if event["event"] == "decision_attempt"}
        results = [event for event in events if event["event"] == "decision_result"]
        self.assertEqual({event["decision"]["action"] for event in results}, {"apply", "undo", "leave"})
        self.assertTrue(all(event["attempt_id"] in attempts and event["outcome"] == "success" for event in results))
        self.assertTrue(all(event["timestamp"] for event in events))
        self.stop()
        self.start()
        self.assertTrue(audit_path.read_bytes().startswith(before_restart))
        self.assertEqual(self.state()["audit_status"], "ok")
        self.arrive("third-invoice.txt")
        third = self.pending("third-invoice.txt")
        saved_log = audit_path.with_suffix(".saved")
        audit_path.rename(saved_log)
        audit_path.mkdir()  # Simulate an unwritable log without relying on user permissions.
        status, _ = self.action(third, "apply", {"folder_id": "records", "name": "Third rental.txt"})
        self.assertLess(status, 300)
        self.assertTrue(self.state()["audit_status"].startswith("error:"))
        self.assertTrue((alternate / "Third rental.txt").exists())
        audit_path.rmdir()
        saved_log.rename(audit_path)
        self.assertTrue(audit_path.read_bytes().startswith(before_restart))
        self.stop()
        with sqlite3.connect(self.state_dir / "state.sqlite3") as database:
            database.execute("DELETE FROM meta WHERE key='audit_enabled_v1'")
        self.start()
        reconstructed = [json.loads(line) for line in audit_path.read_bytes().splitlines()
                         if json.loads(line).get("reconstructed")]
        decisions = [event for event in reconstructed if event["event"] == "decision_result"]
        self.assertTrue(decisions)
        self.assertTrue(all(event["occurred_at"] is None and event["context_historical"] is False
                            for event in decisions))
        after_backfill = audit_path.read_bytes()
        self.stop()
        self.start()
        self.assertEqual(audit_path.read_bytes(), after_backfill)

    @unittest.skipUnless(os.environ.get("DOWNLOAD_SUGGEST_TEST_MODEL") == "1", "Live local model not requested")
    def test_warm_local_model_suggests_four_synthetic_downloads_under_two_seconds(self):
        self.stop()
        config = json.loads(self.config.read_text())
        config["rules"] = []
        config["model"]["enabled"] = True
        config["poll_interval_ms"] = 150
        config["settle_ms"] = 400
        research = self.root / "Documents" / "Research"
        research.mkdir(parents=True)
        config["folders"].append({"id": "research", "label": "Research", "path": str(research),
                                   "description": "Scientific research papers and academic experiments"})
        for category in ("Recipes", "Travel", "Music", "Manuals", "Gardening", "Sports"):
            folder = self.root / "Documents" / category
            folder.mkdir()
            config["folders"].append({"id": category.lower(), "label": category,
                                       "path": str(folder), "description": category + " documents"})
        # A large tree with tempting keyword matches must not hide Research.
        for index in range(10):
            folder = self.destination / f"Paper rental results {index}"
            folder.mkdir()
            config["folders"].append({"id": f"housing-paper-{index}",
                "label": f"Housing / Paper rental results {index}", "path": str(folder),
                "description": "Paper records of apartment experiments and rental inspection results"})
        self.config.write_text(json.dumps(config))
        self.start()
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            status = self.state()["model_status"]
            if status.startswith("TinyJev ready"):
                break
            if "unavailable" in status:
                self.fail("Live model unavailable; install dependencies and cache weights first")
            time.sleep(.2)
        else:
            self.fail("Live model did not warm within two minutes")
        cases = [
            ("rental-agreement.txt", "Apartment rental agreement\nMonthly rent and landlord tenancy terms.", "housing"),
            ("research-paper.txt", "Scientific research paper\nAcademic experiments, methods, and research results.", "research"),
            ("housing-invoice.txt", "Apartment rental invoice\nMonthly rent payable for the tenant apartment.", "housing"),
            ("0421.00123.txt", "Learning communication protocols\nAbstract: This paper studies neural networks for cooperating agents. Our experiments and results advance artificial intelligence.", "research"),
        ]
        latencies = []
        for name, content, expected in cases:
            started = time.monotonic()
            self.arrive(name, content)
            item = self.pending(name)
            elapsed = time.monotonic() - started
            latencies.append(round(elapsed * 1000))
            self.assertEqual(item["method"], "tinyjev")
            self.assertEqual(item["folder_id"], expected)
            self.assertLess(elapsed, 2)
            self.action(item, "leave")
        print("Warm model download-to-suggestion latencies (ms):", latencies)


class SetupFlow(unittest.TestCase):
    def test_configure_excludes_cache_and_repository_internals(self):
        with tempfile.TemporaryDirectory(prefix="setup-e2e-") as directory:
            root = Path(directory).resolve()
            downloads, documents, state = root / "Downloads", root / "Documents", root / "state"
            downloads.mkdir()
            for name in ("Research/Papers", "Research/__pycache__", "Project/.git", "Project/Internal", "node_modules/pkg"):
                (documents / name).mkdir(parents=True)
            subprocess.run([sys.executable, "-m", "download_suggest", "configure", "--rules-only",
                            "--watch-dir", str(downloads), "--documents", str(documents), "--state-dir", str(state)],
                           env=dict(os.environ, PYTHONPATH=str(REPO / "src")), check=True, capture_output=True)
            labels = {folder["label"] for folder in json.loads((state / "config.json").read_text())["folders"]}
            self.assertEqual(labels, {"Project", "Research", "Research / Papers"})
            config_path = state / "config.json"
            config = json.loads(config_path.read_text())
            config["folders"][1]["label"] = config["folders"][0]["label"].upper()
            config_path.write_text(json.dumps(config))
            invalid = subprocess.run([sys.executable, "-m", "download_suggest", "serve", "--config", str(config_path),
                                      "--state-dir", str(state)], env=dict(os.environ, PYTHONPATH=str(REPO / "src")),
                                     capture_output=True, timeout=5)
            self.assertNotEqual(invalid.returncode, 0)
            self.assertFalse((state / "session.json").exists())


class NativeExtractionFlow(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "darwin", "AppKit requires macOS")
    def test_native_folder_browser_interactions(self):
        with tempfile.TemporaryDirectory(prefix="native-ui-e2e-") as directory:
            root = Path(directory)
            source = (REPO / "macos/DownloadSuggest.swift").read_text()
            source = source[:source.rindex("let app = NSApplication.shared")]
            (root / "main.swift").write_text(source + (REPO / "tests/native_ui_checks.swift").read_text())
            subprocess.run(["xcrun", "swiftc", "-swift-version", "6", str(root / "main.swift"),
                            "-o", str(root / "check"), "-framework", "AppKit", "-framework", "UserNotifications",
                            "-framework", "PDFKit"], check=True, capture_output=True, timeout=60)
            result = subprocess.run([str(root / "check")], capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Native folder navigation", result.stdout)


    @unittest.skipUnless(os.environ.get("DOWNLOAD_SUGGEST_TEST_APP"), "Native extractor not supplied")
    def test_native_pdf_excerpt_uses_synthetic_document(self):
        with tempfile.TemporaryDirectory(prefix="pdf-e2e-") as directory:
            path = Path(directory) / "example.pdf"
            stream = b"BT /F1 18 Tf 30 100 Td (Example rental invoice) Tj ET"
            objects = [
                b"<< /Type /Catalog /Pages 2 0 R >>",
                b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
                b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
                b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
                b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
            ]
            data = b"%PDF-1.4\n"
            offsets = [0]
            for index, content in enumerate(objects, 1):
                offsets.append(len(data))
                data += str(index).encode() + b" 0 obj\n" + content + b"\nendobj\n"
            xref = len(data)
            data += b"xref\n0 6\n0000000000 65535 f \n"
            for offset in offsets[1:]:
                data += f"{offset:010d} 00000 n \n".encode()
            data += f"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
            path.write_bytes(data)
            result = subprocess.run([os.environ["DOWNLOAD_SUGGEST_TEST_APP"], "--extract-text", str(path)],
                                    capture_output=True, text=True, timeout=3, check=True)
            self.assertIn("Example rental invoice", json.loads(result.stdout)["text"])


class PrivacyFlow(unittest.TestCase):
    def test_public_guard_rejects_leaks_but_does_not_read_ignored_data(self):
        with tempfile.TemporaryDirectory(prefix="publication-e2e-") as directory:
            root = Path(directory)
            (root / "scripts").mkdir()
            shutil.copy(REPO / "scripts/check_public.py", root / "scripts/check_public.py")
            shutil.copy(REPO / ".gitignore", root / ".gitignore")
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            private = root / ".local"
            private.mkdir()
            sensitive = "/" + "Users" + "/fictional-person/Documents/"
            (private / "config.json").write_text(sensitive)
            command = [sys.executable, str(root / "scripts/check_public.py")]
            result = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            screenshot = root / 'docs/screenshots/review.jpg'
            screenshot.parent.mkdir(parents=True)
            shutil.copy(REPO / 'docs/screenshots/review.jpg', screenshot)
            result = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            with screenshot.open('ab') as stream:
                stream.write(b'unreviewed change')
            result = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(result.returncode, 1)
            self.assertIn('unreviewed screenshot change', result.stderr)
            screenshot.unlink()
            (root / "accidental.txt").write_text(sensitive)
            result = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(result.returncode, 1)
            self.assertIn("absolute home path", result.stderr)
            self.assertNotIn(sensitive, result.stderr + result.stdout)
            subprocess.run(["git", "add", "accidental.txt"], cwd=root, check=True)
            (root / "accidental.txt").write_text("Clean working copy")
            result = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(result.returncode, 1)
            self.assertIn("staged absolute home path", result.stderr)
            self.assertNotIn(sensitive, result.stderr + result.stdout)
            subprocess.run(["git", "rm", "--cached", "-f", "-q", "accidental.txt"], cwd=root, check=True)
            (root / "accidental.txt").unlink()
            for name in ("passport.pdf", "slides.pptx", "capture.png", "export.csv", "credentials.pem"):
                artifact = root / name
                artifact.write_text("Synthetic private document")
                self.assertEqual(subprocess.run(["git", "check-ignore", name], cwd=root, capture_output=True).returncode, 0)
                subprocess.run(["git", "add", "-f", name], cwd=root, check=True)
                result = subprocess.run(command, capture_output=True, text=True)
                self.assertEqual(result.returncode, 1)
                self.assertIn("private file or document", result.stderr)
                subprocess.run(["git", "rm", "--cached", "-q", name], cwd=root, check=True)
                artifact.unlink()
            (root / "recommendations.jsonl").write_text('{"event":"synthetic decision"}\n')
            ignored = subprocess.run(["git", "check-ignore", "recommendations.jsonl"], cwd=root,
                                     capture_output=True, text=True)
            self.assertEqual(ignored.returncode, 0)
            subprocess.run(["git", "add", "-f", "recommendations.jsonl"], cwd=root, check=True)
            result = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(result.returncode, 1)
            self.assertIn("private file or document", result.stderr)


if __name__ == "__main__":
    unittest.main()
