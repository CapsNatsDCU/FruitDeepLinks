import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

from xtream_curl import CurlStream
from xtream_process import close_media_process


class CurlStreamTest(unittest.TestCase):
    def test_close_kills_child_when_terminate_fails(self):
        process = Mock()
        process.poll.return_value = None
        process.terminate.side_effect = OSError("terminate failed")
        process.stdin.closed = False
        process.stdout.closed = False
        close_media_process(process)
        process.kill.assert_called_once()
        process.wait.assert_called_once_with(timeout=5)
        process.stdin.close.assert_called_once()
        process.stdout.close.assert_called_once()

    def test_authenticated_url_is_sent_only_through_stdin_configuration(self):
        process = Mock()
        process.stdin = Mock()
        process.stdin.closed = False
        process.stdout = Mock()
        process.stdout.closed = False
        process.poll.return_value = 0
        with patch("xtream_curl.subprocess.Popen", return_value=process) as popen:
            stream = CurlStream("http://provider.example/live/private-user/private-password/7.ts", 5, 42)
        command = popen.call_args.args[0]
        self.assertEqual(command[:4], ["curl", "--silent", "--location", "--fail"])
        self.assertNotIn("-4", command)
        self.assertNotIn("private-user", " ".join(command))
        self.assertNotIn("private-password", " ".join(command))
        process.stdin.write.assert_called_once()
        written = process.stdin.write.call_args.args[0]
        self.assertIn(b"private-user", written)
        self.assertIn(b"private-password", written)
        self.assertEqual((42,), popen.call_args.kwargs["pass_fds"])
        stream.close()


if __name__ == "__main__":
    unittest.main()
