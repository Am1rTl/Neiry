"""Run the test suite with Capsule stubs and temporary session state."""
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from _sdk_stubs import install


def main() -> None:
    with TemporaryDirectory(prefix="neuroscope-tests-") as temporary:
        workspace = Path(temporary)
        with patch.dict("os.environ", {"RECORDS_DIR": str(workspace / "records")}):
            install()
            import server

            with (
                patch.object(server, "SNAPSHOT_PATH", str(workspace / "state_snapshot.json")),
                patch.object(server.LAB_STORE, "reports", workspace / "reports"),
            ):
                suite = unittest.defaultTestLoader.discover(".", pattern="test_*.py")
                result = unittest.TextTestRunner().run(suite)
    raise SystemExit(not result.wasSuccessful())


if __name__ == "__main__":
    main()
