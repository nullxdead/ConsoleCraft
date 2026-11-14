"""Runtime requirements and download boundaries, without depending on local Java."""

from pathlib import Path
import subprocess
import unittest
from unittest.mock import Mock, patch

from consolecraft.java import (
    JavaError, JavaRuntime, ensure_java, find_java, java_major, required_java_major,
)


class JavaTests(unittest.TestCase):
    def test_manifest_requirement_takes_precedence_for_future_versions(self):
        self.assertEqual(required_java_major({"id": "future-snapshot", "javaVersion": {"majorVersion": 25}}), 25)
        for version, expected in [("1.12.2", 8), ("1.17.1", 16), ("1.20.4", 17), ("1.20.5", 21)]:
            with self.subTest(version=version):
                self.assertEqual(required_java_major({"id": version}), expected)

    def test_java_version_outputs_from_old_and_modern_jvms(self):
        outputs = [("java version \"1.8.0_462\"", 8), ("openjdk version \"17.0.16\" 2025-07-15", 17),
                   ("openjdk 21.0.8 2025-07-15 LTS", 21), ("openjdk version \"25-ea\"", 25)]
        for output, expected in outputs:
            with self.subTest(output=output), patch("consolecraft.java.subprocess.run", return_value=Mock(
                returncode=0, stdout="", stderr=output,
            )) as run:
                self.assertEqual(java_major("/path with spaces/java"), expected)
                self.assertEqual(run.call_args.args[0], ["/path with spaces/java", "-version"])
                self.assertFalse(run.call_args.kwargs.get("shell", False))

    def test_missing_and_hung_java_are_not_accepted(self):
        for exception in [FileNotFoundError(), subprocess.TimeoutExpired("java", 10)]:
            with self.subTest(exception=type(exception).__name__), patch(
                "consolecraft.java.subprocess.run", side_effect=exception,
            ):
                self.assertIsNone(java_major("java"))

    def test_finder_uses_matching_major_instead_of_first_available(self):
        with patch("consolecraft.java._candidates", return_value=["java25", "java8"]), patch(
            "consolecraft.java.java_major", side_effect=[25, 8],
        ):
            self.assertEqual(find_java(Path("game"), {"id": "1.12.2"}), JavaRuntime("java8", 8))

    def test_explicit_incompatible_java_reports_requirement(self):
        with patch("consolecraft.java.java_major", return_value=25), patch(
            "consolecraft.java.shutil.which", return_value="custom-java",
        ):
            with self.assertRaisesRegex(JavaError, "Java 8.*Java 25"):
                find_java(Path("game"), {"id": "1.12.2"}, configured="custom-java")

    def test_local_java_never_triggers_runtime_download(self):
        local = JavaRuntime("local-java", 21)
        with patch("consolecraft.java.find_java", return_value=local), patch(
            "consolecraft.java.runtime.install_jvm_runtime",
        ) as download:
            self.assertEqual(ensure_java(Path("game"), {"id": "1.21"}), local)
            download.assert_not_called()

    def test_offline_java_failure_never_triggers_runtime_download(self):
        with patch("consolecraft.java.find_java", side_effect=JavaError("not found")), patch(
            "consolecraft.java.runtime.install_jvm_runtime",
        ) as download:
            with self.assertRaisesRegex(JavaError, "not found"):
                ensure_java(Path("game"), {"id": "1.21"}, download=False)
            download.assert_not_called()

    def test_missing_runtime_is_installed_then_verified(self):
        metadata = {"javaVersion": {"component": "java-runtime-delta", "majorVersion": 21}}
        local = JavaRuntime("downloaded-java", 21)
        callback = {"setStatus": Mock()}
        with patch("consolecraft.java.find_java", side_effect=[JavaError("missing"), local]), patch(
            "consolecraft.java.runtime.install_jvm_runtime",
        ) as download:
            self.assertEqual(ensure_java(Path("game"), metadata, callback=callback), local)
            download.assert_called_once_with("java-runtime-delta", Path("game"), callback=callback)

    def test_unsafe_runtime_component_is_rejected(self):
        with patch("consolecraft.java.find_java", side_effect=JavaError("missing")), patch(
            "consolecraft.java.runtime.install_jvm_runtime",
        ) as download:
            with self.assertRaises(JavaError):
                ensure_java(Path("game"), {"javaVersion": {"component": "../outside", "majorVersion": 21}})
            download.assert_not_called()


if __name__ == "__main__":
    unittest.main()
