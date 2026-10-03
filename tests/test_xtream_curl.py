import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

from xtream_curl import CurlStream


class CurlStreamTest(unittest.TestCase):
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
