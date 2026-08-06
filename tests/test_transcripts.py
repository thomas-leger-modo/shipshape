from __future__ import annotations

import unittest
from contextlib import redirect_stdout
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from shipshape.transcripts.cli import Hit, main, shell_init
from shipshape.transcripts.scan import Session


class TranscriptTests(unittest.TestCase):
    def test_resume_command_quotes_shell_arguments(self) -> None:
        session = Session(
            agent="claude",
            path=Path("/tmp/transcript.jsonl"),
            session_id="session;id",
            cwd="/tmp/a project",
            label="Example",
            started=datetime.now(UTC),
        )

        self.assertEqual(
            session.resume_command,
            "cd '/tmp/a project' && claude --resume 'session;id'",
        )

    def test_shell_init_queues_selected_command_in_next_prompt(self) -> None:
        integration = shell_init()

        self.assertIn('command search-transcripts --print-command "$@"', integration)
        self.assertIn('print -rz -- "$resume_command"', integration)

    def test_print_command_mode_outputs_only_resume_command(self) -> None:
        session = Session(
            agent="pi",
            path=Path("/tmp/transcript.jsonl"),
            session_id="session-id",
            cwd="/tmp/project",
            label="Example",
            started=datetime.now(UTC),
        )
        hit = Hit(session=session, turns=[], total=2)
        output = StringIO()

        with (
            patch("sys.argv", ["search-transcripts", "--print-command", "needle"]),
            patch("shipshape.transcripts.cli.require"),
            patch("shipshape.transcripts.cli.search", return_value=[hit]),
            patch("shipshape.transcripts.cli.select", return_value=hit),
            redirect_stdout(output),
        ):
            status = main()

        self.assertEqual(status, 0)
        self.assertEqual(output.getvalue(), "pi --session session-id\n")


if __name__ == "__main__":
    unittest.main()
