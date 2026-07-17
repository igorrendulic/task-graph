import tempfile
import unittest
from pathlib import Path

from scripts.task_graph_board import move_task, render_kanban


class TaskGraphBoardTests(unittest.TestCase):
    def test_move_task_updates_column_and_renders_kanban(self):
        with tempfile.TemporaryDirectory() as temp:
            plan = Path(temp)
            todo = plan / "todo"
            todo.mkdir()
            (todo / "001-first.md").write_text("# First")

            move_task(plan, "001-first.md", "in-progress")
            render_kanban(plan)

            self.assertFalse((todo / "001-first.md").exists())
            self.assertTrue((plan / "in-progress" / "001-first.md").exists())
            self.assertIn("001-first.md", (plan / "kanban.md").read_text())
