"""Tests for zdrovena.common.shipping_state — draft lifecycle transitions (R5-A)."""

from __future__ import annotations

import pytest

from zdrovena.common import shipping_state as s


class TestCanExecute:
    @pytest.mark.parametrize("state", [s.PENDING, s.PENDING_CONFIRMATION, s.ERROR])
    def test_executable_states(self, state):
        assert s.can_execute(state) is True

    @pytest.mark.parametrize("state", [s.CREATED, s.EXECUTING, s.CANCELLED, s.NEEDS_REVIEW, None])
    def test_non_executable_states(self, state):
        assert s.can_execute(state) is False


class TestCanTransition:
    def test_claim_transition_allowed(self):
        assert s.can_transition(s.PENDING, s.EXECUTING) is True
        assert s.can_transition(s.ERROR, s.EXECUTING) is True

    def test_executing_to_terminal(self):
        assert s.can_transition(s.EXECUTING, s.CREATED) is True
        assert s.can_transition(s.EXECUTING, s.ERROR) is True

    def test_invalid_transitions_rejected(self):
        assert s.can_transition(s.CREATED, s.EXECUTING) is False
        assert s.can_transition(s.CANCELLED, s.EXECUTING) is False
        assert s.can_transition(s.CANCELLED, s.CREATED) is False
        assert s.can_transition(None, s.EXECUTING) is False
