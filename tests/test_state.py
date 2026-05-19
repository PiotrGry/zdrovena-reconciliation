"""Tests for zdrovena.month_closing.state.PipelineState."""

from __future__ import annotations

import json

from zdrovena.month_closing.state import PipelineState


class TestPipelineState:
    def test_new_state_has_no_completed(self, tmp_path):
        state = PipelineState(tmp_path)
        assert state.completed_steps == []

    def test_mark_done(self, tmp_path):
        state = PipelineState(tmp_path)
        state.mark_done("step_1")
        assert state.is_done("step_1")
        assert not state.is_done("step_2")

    def test_mark_done_idempotent(self, tmp_path):
        state = PipelineState(tmp_path)
        state.mark_done("step_1")
        state.mark_done("step_1")
        assert state.completed_steps.count("step_1") == 1

    def test_persists_to_file(self, tmp_path):
        state = PipelineState(tmp_path)
        state.mark_done("step_1")
        state.mark_done("step_2")

        # Re-create from same directory
        state2 = PipelineState(tmp_path)
        assert state2.is_done("step_1")
        assert state2.is_done("step_2")

    def test_state_file_is_json(self, tmp_path):
        state = PipelineState(tmp_path)
        state.mark_done("Pre-flight")

        data = json.loads(state.path.read_text())
        assert "completed_steps" in data
        assert "Pre-flight" in data["completed_steps"]

    def test_reset(self, tmp_path):
        state = PipelineState(tmp_path)
        state.mark_done("step_1")
        state.reset()

        assert state.completed_steps == []
        assert not state.path.exists()

    def test_corrupt_state_file(self, tmp_path):
        state_file = tmp_path / ".state.json"
        state_file.write_text("{{not json!!")

        state = PipelineState(tmp_path)
        assert state.completed_steps == []  # starts fresh

    def test_completed_steps_returns_copy(self, tmp_path):
        state = PipelineState(tmp_path)
        state.mark_done("step_1")
        steps = state.completed_steps
        steps.append("fake")
        assert "fake" not in state.completed_steps

    def test_reset_with_blob_storage(self, tmp_path):
        """reset() deletes blob checkpoint when storage is provided."""
        from unittest.mock import MagicMock

        storage = MagicMock()
        state = PipelineState(
            tmp_path, storage=storage, blob_key="faktury/2026/kwiecien/.state.json"
        )
        state.mark_done("step_1")
        state.reset()

        storage.delete.assert_called_once_with("faktury/2026/kwiecien/.state.json")
        assert state.completed_steps == []

    def test_load_from_blob(self, tmp_path):
        """_load() reads from blob storage when available."""
        import json
        from unittest.mock import MagicMock

        payload = json.dumps({"completed_steps": ["Pre-flight", "ZIP archive"]})
        blob_key = "faktury/2026/kwiecien/.state.json"

        storage = MagicMock()
        storage.exists.return_value = True
        storage.download.side_effect = lambda key, dest: dest.write_text(payload)

        state = PipelineState(tmp_path, storage=storage, blob_key=blob_key)
        assert "Pre-flight" in state.completed_steps
        assert "ZIP archive" in state.completed_steps

    def test_save_mirrors_to_blob(self, tmp_path):
        """mark_done() syncs to blob storage when configured."""
        from unittest.mock import MagicMock

        storage = MagicMock()
        state = PipelineState(
            tmp_path, storage=storage, blob_key="faktury/2026/kwiecien/.state.json"
        )
        state.mark_done("Pre-flight")

        assert storage.upload_stream.called

    def test_reset_blob_delete_failure_is_silent(self, tmp_path):
        """reset() continues even if blob delete fails — just logs a warning."""
        from unittest.mock import MagicMock

        storage = MagicMock()
        storage.delete.side_effect = RuntimeError("blob unavailable")
        state = PipelineState(
            tmp_path, storage=storage, blob_key="faktury/2026/kwiecien/.state.json"
        )
        state.mark_done("step_1")
        state.reset()  # must not raise

        assert state.completed_steps == []
