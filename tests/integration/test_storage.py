from appwrite.enums.compression import Compression
from support import LiveIntegrationTestCase, requires_live_integration


@requires_live_integration
class StorageIntegrationTests(LiveIntegrationTestCase):
    def test_storage_smoke(self):
        runner = self.new_runner()
        bucket_id = runner.unique_id("bucket")
        file_id = runner.unique_id("file")
        png_path = runner.create_png_file()

        try:
            runner.call("storage_list_buckets")
            runner.call(
                "storage_create_bucket",
                {
                    "bucket_id": bucket_id,
                    "name": "MCP Smoke Bucket",
                    "file_security": True,
                    "compression": Compression.NONE.value,
                    "encryption": True,
                    "antivirus": True,
                },
            )
            runner.call("storage_get_bucket", {"bucket_id": bucket_id})
            runner.call(
                "storage_create_file",
                {"bucket_id": bucket_id, "file_id": file_id, "file": png_path},
            )
            runner.call(
                "storage_get_file_preview", {"bucket_id": bucket_id, "file_id": file_id}
            )
        finally:
            try:
                runner.call(
                    "storage_delete_file", {"bucket_id": bucket_id, "file_id": file_id}
                )
            except Exception:
                pass
            try:
                runner.call("storage_delete_bucket", {"bucket_id": bucket_id})
            except Exception:
                pass

        self.assert_no_unexpected_errors(runner)
