import hashlib
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import app as panel


class ObfuscationBackupApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repos_dir = os.path.join(self.tmp.name, "repos")
        self.data_dir = os.path.join(self.tmp.name, "data")
        self.backup_dir = os.path.join(self.data_dir, "obfuscation_backups")
        os.makedirs(self.repos_dir)
        os.makedirs(self.data_dir)

        self.repo_names = {"repo-a", "repo-b"}
        self.branches = {
            "repo-a": ["main", "dev"],
            "repo-b": ["main"],
        }
        for repo in self.repo_names:
            root = os.path.join(self.repos_dir, repo)
            os.makedirs(os.path.join(root, ".git"))
            with open(os.path.join(root, "app.js"), "w", encoding="utf-8") as handle:
                handle.write("const NAME = 'demo';\nconsole.log(NAME);\n")
            with open(os.path.join(root, "app.py"), "w", encoding="utf-8") as handle:
                handle.write("NAME = 'demo'\nprint(NAME)\n")

        self.patchers = [
            patch.object(panel, "REPOS_DIR", self.repos_dir),
            patch.object(panel, "DATA_DIR", self.data_dir),
            patch.object(panel, "OBFUSCATION_BACKUP_DIR", self.backup_dir),
            patch.object(
                panel,
                "load_repos",
                side_effect=lambda: [
                    {"name": name, "url": "https://example.invalid/repo.git"}
                    for name in sorted(self.repo_names)
                ],
            ),
            patch.object(
                panel,
                "list_branches",
                side_effect=lambda name: list(self.branches.get(name, [])),
            ),
        ]
        for patcher in self.patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

        panel._OBFUSCATION_BACKUP_LOCKS.clear()
        panel.app.config.update(TESTING=True, SECRET_KEY="test-secret")
        self.client = panel.app.test_client()

    def login(self):
        with self.client.session_transaction() as session:
            session["logged_in"] = True

    def payload(self, **overrides):
        data = {
            "repo": "repo-a",
            "branch": "main",
            "file": "app.js",
        }
        data.update(overrides)
        return data

    def create(self, content, **overrides):
        return self.client.post(
            "/api/obfuscation-backups",
            json=self.payload(action="create", content=content, **overrides),
        )

    def test_login_is_required(self):
        response = self.client.get(
            "/api/obfuscation-backups",
            query_string=self.payload(action="list"),
        )
        self.assertEqual(response.status_code, 401)
        self.assertFalse(response.get_json()["ok"])

    def test_create_uses_beijing_time_name_sha_and_same_second_sequence(self):
        self.login()
        fixed = datetime(2026, 9, 26, 11, 8, 27, tzinfo=timezone(timedelta(hours=8)))
        content = "const NAME = 'alpha';\nconsole.log(NAME);\n"
        with patch.object(panel, "_beijing_now", return_value=fixed):
            first = self.create(content)
            second = self.create(content)

        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 201)
        first_backup = first.get_json()["backup"]
        second_backup = second.get_json()["backup"]
        self.assertEqual(first_backup["filename"], "alpha_2026-09-26_11-08-27.js")
        self.assertEqual(second_backup["filename"], "alpha_2026-09-26_11-08-27_2.js")
        self.assertEqual(first_backup["sha256"], hashlib.sha256(content.encode()).hexdigest())
        self.assertFalse(first_backup["same_as_previous"])
        self.assertTrue(second_backup["same_as_previous"])
        self.assertTrue(first_backup["created_at"].endswith("+08:00"))

    def test_path_traversal_is_rejected(self):
        self.login()
        response = self.create("print('x')\n", file="../outside.py")
        self.assertEqual(response.status_code, 400)
        self.assertIn("非法文件路径", response.get_json()["error"])

    def test_five_megabyte_limit(self):
        self.login()
        allowed = "x" * panel.OBFUSCATION_BACKUP_MAX_BYTES
        accepted = self.create(allowed)
        self.assertEqual(accepted.status_code, 201)

        rejected = self.create(allowed + "x")
        self.assertEqual(rejected.status_code, 400)
        self.assertIn("5MB", rejected.get_json()["error"])

    def test_list_view_download_restore_and_delete(self):
        self.login()
        content = "const NAME = 'roundtrip';\n"
        created_response = self.create(content)
        self.assertEqual(created_response.status_code, 201)
        created = created_response.get_json()["backup"]
        backup_id = created["id"]

        listed = self.client.get(
            "/api/obfuscation-backups",
            query_string=self.payload(action="list"),
        )
        self.assertEqual(listed.status_code, 200)
        self.assertEqual([item["id"] for item in listed.get_json()["backups"]], [backup_id])

        for action in ("view", "restore"):
            response = self.client.post(
                "/api/obfuscation-backups",
                json=self.payload(action=action, id=backup_id),
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.get_json()["content"], content)

        downloaded = self.client.get(
            "/api/obfuscation-backups",
            query_string=self.payload(action="download", id=backup_id),
        )
        self.assertEqual(downloaded.status_code, 200)
        self.assertEqual(downloaded.data, content.encode("utf-8"))
        self.assertIn(created["filename"], downloaded.headers["Content-Disposition"])

        deleted = self.client.post(
            "/api/obfuscation-backups",
            json=self.payload(action="delete", id=backup_id),
        )
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(deleted.get_json()["deleted"], backup_id)

        missing = self.client.post(
            "/api/obfuscation-backups",
            json=self.payload(action="view", id=backup_id),
        )
        self.assertEqual(missing.status_code, 404)

    def test_repo_and_branch_scopes_are_isolated(self):
        self.login()
        main_id = self.create("const NAME = 'main';\n").get_json()["backup"]["id"]
        dev_id = self.create(
            "const NAME = 'dev';\n", branch="dev"
        ).get_json()["backup"]["id"]
        other_id = self.create(
            "const NAME = 'other';\n", repo="repo-b"
        ).get_json()["backup"]["id"]

        main_list = self.client.get(
            "/api/obfuscation-backups",
            query_string=self.payload(action="list"),
        ).get_json()["backups"]
        dev_list = self.client.get(
            "/api/obfuscation-backups",
            query_string=self.payload(action="list", branch="dev"),
        ).get_json()["backups"]
        other_list = self.client.get(
            "/api/obfuscation-backups",
            query_string=self.payload(action="list", repo="repo-b"),
        ).get_json()["backups"]

        self.assertEqual([item["id"] for item in main_list], [main_id])
        self.assertEqual([item["id"] for item in dev_list], [dev_id])
        self.assertEqual([item["id"] for item in other_list], [other_id])

        cross_scope = self.client.post(
            "/api/obfuscation-backups",
            json=self.payload(action="view", branch="dev", id=main_id),
        )
        self.assertEqual(cross_scope.status_code, 404)

    def test_rotation_keeps_only_latest_ten_backups(self):
        self.login()
        ids = []
        for index in range(12):
            response = self.create(f"const NAME = 'item-{index}';\n")
            self.assertEqual(response.status_code, 201)
            ids.append(response.get_json()["backup"]["id"])

        listed = self.client.get(
            "/api/obfuscation-backups",
            query_string=self.payload(action="list"),
        ).get_json()["backups"]
        listed_ids = [item["id"] for item in listed]
        self.assertEqual(len(listed_ids), 10)
        self.assertEqual(listed_ids, list(reversed(ids[-10:])))

        for removed_id in ids[:2]:
            response = self.client.post(
                "/api/obfuscation-backups",
                json=self.payload(action="view", id=removed_id),
            )
            self.assertEqual(response.status_code, 404)

    def test_identical_content_is_marked_against_previous_backup(self):
        self.login()
        content = "const NAME = 'same';\n"
        first = self.create(content).get_json()["backup"]
        second = self.create(content).get_json()["backup"]
        third = self.create("const NAME = 'different';\n").get_json()["backup"]

        self.assertFalse(first["same_as_previous"])
        self.assertTrue(second["same_as_previous"])
        self.assertFalse(third["same_as_previous"])


if __name__ == "__main__":
    unittest.main()
