import contextlib
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.request import urlopen

from agent_stats.cli import main
from agent_stats.core import Store
from agent_stats.demo import generate
from agent_stats.server import make_server


class IntegrationTests(unittest.TestCase):
    def test_demo_import_restart_and_privacy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            generate(source)
            database = root / "data.sqlite3"
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(["--db", str(database), "import", "--source", str(source)]), 0)
                self.assertEqual(main(["--db", str(database), "import", "--source", str(source)]), 0)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(main(["--db", str(database), "summary"]), 0)
            data = json.loads(output.getvalue())
            self.assertEqual(data["summary"]["sessions"], 12)
            self.assertEqual(data["summary"]["turns"], 30)
            self.assertIsNone(data["summary"]["token_total"])
            self.assertEqual(data["summary"]["token_known"], 336000)
            self.assertIsNone(data["summary"]["cost"])
            self.assertNotIn("SYNTHETIC_SECRET_DO_NOT_PERSIST", output.getvalue())
            self.assertNotIn(b"SYNTHETIC_SECRET_DO_NOT_PERSIST", database.read_bytes())
            store = Store(database)
            again = store.query()
            self.assertEqual(again["summary"]["token_total"], data["summary"]["token_total"])
            selected = store.query({"model": "demo-model-a"})
            self.assertTrue(selected["turns"])
            self.assertTrue(all(t["model"] == "demo-model-a" for t in selected["turns"]))

    def test_real_store_across_http_thread(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            generate(root / "source")
            store = Store(root / "data.sqlite3")
            store.import_paths([str(root / "source")])
            server = make_server(store, [str(root / "source")], 0)
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                with urlopen("http://127.0.0.1:%s/api/overview" % server.server_port, timeout=5) as response:
                    data = json.load(response)
                self.assertEqual(data["summary"]["sessions"], 12)
                self.assertEqual(data["summary"]["token_known"], 336000)
            finally:
                server.shutdown()
                server.server_close()
                worker.join()


if __name__ == "__main__":
    unittest.main()
