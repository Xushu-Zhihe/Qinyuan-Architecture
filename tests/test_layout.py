import tempfile
import unittest
from pathlib import Path

from orchestrator.layout import LayoutConflict, LayoutStore


class LayoutStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = LayoutStore(Path(self.temporary.name))

    def test_default_layout_has_revision_zero(self) -> None:
        self.assertEqual(
            self.store.load(),
            {
                "revision": 0,
                "nodes": {},
                "edges": {},
                "viewport": {},
            },
        )

    def test_layout_update_requires_current_revision(self) -> None:
        first = self.store.save(
            0,
            {
                "nodes": {"fact-1": {"x": 10, "y": 20}},
                "edges": {},
                "viewport": {"x": 0, "y": 0, "zoom": 1},
            },
        )

        self.assertEqual(first["revision"], 1)
        with self.assertRaises(LayoutConflict) as caught:
            self.store.save(
                0,
                {"nodes": {}, "edges": {}, "viewport": {}},
            )
        self.assertEqual(caught.exception.current_revision, 1)
        self.assertEqual(self.store.load()["revision"], 1)


if __name__ == "__main__":
    unittest.main()
