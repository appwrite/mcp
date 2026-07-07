import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mcp_server_appwrite import constants


def _load_render_module():
    script_path = Path(__file__).parents[2] / "scripts" / "render_server_json.py"
    spec = importlib.util.spec_from_file_location("render_server_json", script_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Unable to load render_server_json.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ServerVersionTests(unittest.TestCase):
    def test_resolve_server_version_uses_package_metadata(self):
        with patch.object(
            constants.importlib_metadata, "version", return_value="1.2.3"
        ):
            self.assertEqual(constants._resolve_server_version(), "1.2.3")

    def test_resolve_server_version_falls_back_when_metadata_missing(self):
        with patch.object(
            constants.importlib_metadata,
            "version",
            side_effect=constants.importlib_metadata.PackageNotFoundError,
        ):
            self.assertEqual(constants._resolve_server_version(), "0.0.0+unknown")


class RenderServerMetadataTests(unittest.TestCase):
    def test_render_server_metadata_sets_all_release_versions(self):
        module = _load_render_module()

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            template_path = tmp_path / "server.template.json"
            output_path = tmp_path / "server.json"
            template_path.write_text(
                json.dumps(
                    {
                        "version": "__VERSION__",
                        "packages": [
                            {
                                "identifier": "mcp-server-appwrite",
                                "version": "__VERSION__",
                            }
                        ],
                    }
                )
            )

            module.render_server_metadata(
                "1.2.3",
                template_path=template_path,
                output_path=output_path,
            )

            rendered = json.loads(output_path.read_text())
            self.assertEqual(rendered["version"], "1.2.3")
            self.assertEqual(rendered["packages"][0]["version"], "1.2.3")

    def test_render_server_metadata_rejects_non_release_version(self):
        module = _load_render_module()

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            template_path = tmp_path / "server.template.json"
            output_path = tmp_path / "server.json"
            template_path.write_text('{"version": "__VERSION__", "packages": []}')

            with self.assertRaises(ValueError):
                module.render_server_metadata(
                    "1.2.3.dev1",
                    template_path=template_path,
                    output_path=output_path,
                )


if __name__ == "__main__":
    unittest.main()
