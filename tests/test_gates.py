"""Behavioral tests for verification, policy gates, and Git secret scanning."""

import concurrent.futures
import contextlib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from unittest import mock

from agent_dispatch import gates, git_ops, process
from tests.helpers import IsolatedDispatchCase, _git, _write_exec


class TestVerification(IsolatedDispatchCase):
    def test_justfile_and_makefile_variable_assignments_not_misdetected(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "Makefile").write_text("verify := echo fake\ncheck-all:\n\techo nope\n", encoding="utf-8")
            self.assertIsNone(gates.detect_verify_command(d, None))
            (d / "Justfile").write_text('verify := "nope"\ncheck-all:\n    echo nope\n', encoding="utf-8")
            self.assertIsNone(gates.detect_verify_command(d, None))
            (d / "Justfile").write_text('verify := "nope"\nverify:\n    pytest\n', encoding="utf-8")
            self.assertEqual(gates.detect_verify_command(d, None), "just verify")

    def test_verify_cleanup_preserves_existing_files_on_success_and_failure(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = root / "repo"
            repo.mkdir()
            _git(repo, "init", "-b", "main")
            _git(repo, "config", "user.name", "Test")
            _git(repo, "config", "user.email", "test@example.invalid")
            tracked = repo / "__pycache__" / "fixture"
            tracked.parent.mkdir()
            tracked.write_text("keep")
            _git(repo, "add", "-f", "__pycache__/fixture")
            _git(repo, "commit", "-m", "fixture")
            existing = repo / "setup-input"
            existing.write_text("keep")
            for suffix, success in [("", True), ("; echo bad > __pycache__/fixture; exit 2", False)]:
                result = gates.run_verify_gate(
                    repo, "test -f setup-input && touch generated.log" + suffix,
                    allow_no_verify=False, log_file=root / "verify.log", repo_key="verify-clean",
                )
                self.assertEqual(result["passed"], success, result)
                self.assertFalse((repo / "generated.log").exists())
                self.assertEqual(tracked.read_text(), "keep")
                self.assertEqual(existing.read_text(), "keep")

    def test_polyglot_verify_runs_python_after_javascript(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            tests = root / "tests"
            tests.mkdir()
            (tests / "test_backend.py").write_text(
                "import unittest\nclass TestBackend(unittest.TestCase):\n    def test_fail(self): self.fail('backend check')\n"
            )
            (root / "package.json").write_text('{"scripts":{"test":"node js-test.js"}}')
            _write_exec(root / "npm", "#!/bin/sh\ntouch js-ran\n")
            (root / "Cargo.toml").touch()
            (root / "go.mod").touch()
            with mock.patch.dict(os.environ, {"PATH": str(root) + os.pathsep + os.environ["PATH"]}):
                command = gates.detect_verify_command(root, None)
                self.assertIn("go test ./...", command)
                self.assertIn("cargo test", command)
                (root / "Cargo.toml").unlink()
                (root / "go.mod").unlink()
                result = subprocess.run(gates.detect_verify_command(root, None), cwd=root, shell=True, capture_output=True)
            self.assertTrue((root / "js-ran").exists())
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(b"backend check", result.stderr)

    def test_mixed_and_zero_collection_cannot_pass_verify(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            tests = root / "tests"
            tests.mkdir()
            case = tests / "test_case.py"
            with mock.patch.object(shutil, "which", return_value=None):
                for source in ("class TestPlain:\n    def test_fail(self): assert False\n",
                               "async def test_fail(): assert False\n"):
                    case.write_text(source)
                    self.assertIsNone(gates.detect_verify_command(root, None))
                case.write_text("from unittest import TestCase as Case\nclass Base(Case): pass\nclass TestApp(Base): pass\n")
                cmd = gates.detect_verify_command(root, None)
                self.assertIsNotNone(cmd)
                _git(root, "init", "-b", "main")
                with mock.patch.dict(os.environ, {"AGENT_DISPATCH_CACHE_ROOT": str(root / "cache")}):
                    # Python versions before 3.12 exit successfully after collecting zero tests.
                    result = gates.run_verify_gate(root, f"{cmd} || true", allow_no_verify=False, log_file=root / "cache/log",
                                                repo_key="test-zero")
                self.assertFalse(result["passed"])
                self.assertIn("zero tests", result["output"])
            case.unlink()
            (root / "pyproject.toml").touch()
            (root / "test_app.py").write_text("def test_fail(): assert False")
            with mock.patch.object(shutil, "which", side_effect=lambda name: "/mock/ruff" if name == "ruff" else None):
                self.assertIsNone(gates.detect_verify_command(root, None))
            tests.rmdir()
            (root / "src").mkdir()
            (root / "src" / "thing.spec.tsx").touch()
            (root / "package.json").write_text('{"scripts":{"lint":"eslint ."}}')
            self.assertIsNone(gates.detect_verify_command(root, None))

    def test_detection_rejects_mixed_python_and_js_without_a_test_script(self) -> None:
        # Mixed Python unittest+pytest repo MUST NOT silently fall back to unittest discover; JS lint-only with tests/ returns None!
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            py_repo = d / "py_mixed"
            (py_repo / "tests").mkdir(parents=True)
            (py_repo / "tests" / "test_mix.py").write_text(
                "import unittest\nclass T(unittest.TestCase):\n    def test_ok(self): self.assertEqual(1,1)\n"
                "def test_pytest_fail():\n    assert False\n",
                encoding="utf-8",
            )
            with mock.patch.object(shutil, "which", return_value=None):
                self.assertIsNone(gates.detect_verify_command(py_repo, None))

            js_repo = d / "js_lint_only"
            (js_repo / "tests").mkdir(parents=True)
            (js_repo / "package.json").write_text('{"scripts": {"lint": "eslint ."}}', encoding="utf-8")
            self.assertIsNone(gates.detect_verify_command(js_repo, None))

    def test_polyglot_python_tests_dir_does_not_block_frontend_lint(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            # Polyglot repo: frontend package.json has lint (no JS tests) + Python tests/test_api.py
            (root / "package.json").write_text('{"scripts":{"lint":"eslint ."}}', encoding="utf-8")
            (root / "tests").mkdir()
            (root / "tests" / "test_api.py").write_text(
                "import unittest\nclass T(unittest.TestCase):\n    def test_ok(self): self.assertTrue(True)\n",
                encoding="utf-8",
            )
            cmd = gates.detect_verify_command(root, None)
            self.assertEqual(cmd, "npm run lint && python3 -m unittest discover -s tests")


class TestBaseline(IsolatedDispatchCase):
    def test_baseline_cache_uses_detected_recipe_on_target(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = root / "repo"
            repo.mkdir()
            _git(repo, "init", "-b", "main")
            _git(repo, "config", "user.name", "Test")
            _git(repo, "config", "user.email", "test@example.invalid")
            (repo / "pyproject.toml").touch()
            (repo / "tests").mkdir()
            (repo / "tests/test_ok.py").write_text(
                "import unittest\nclass T(unittest.TestCase):\n    def test_ok(self): pass\n"
            )
            _git(repo, "add", ".")
            _git(repo, "commit", "-m", "base")
            base = _git(repo, "rev-parse", "HEAD")
            _git(repo, "commit", "--allow-empty", "-m", "feature")
            args = (repo, "r", repo, base, None, root / "run")
            with mock.patch.object(shutil, "which", return_value=None):
                first = gates.check_base_baseline(*args)
                self.assertTrue(first["passed"])
                self.assertIn("unittest", first["command"])
                self.assertTrue(gates.check_base_baseline(*args)["cached"])
            _write_exec(root / "pytest", "#!/bin/sh\necho newly-selected-pytest >&2\nexit 1\n")
            with mock.patch.dict(os.environ, {"PATH": str(root) + os.pathsep + os.environ["PATH"]}), mock.patch.object(
                shutil, "which", side_effect=lambda name: str(root / "pytest") if name == "pytest" else None
            ):
                changed = gates.check_base_baseline(*args)
            self.assertFalse(changed["cached"])
            self.assertFalse(changed["passed"])
            self.assertIn("newly-selected-pytest", changed["output"])

    def test_baseline_cache_distinguishes_setup_and_rejects_changed_head(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = root / "repo"
            repo.mkdir()
            _git(repo, "init", "-b", "main")
            _git(repo, "config", "user.email", "test@example.invalid")
            _git(repo, "config", "user.name", "Test")
            _git(repo, "commit", "--allow-empty", "-m", "base")
            base = _git(repo, "rev-parse", "HEAD")
            _git(repo, "commit", "--allow-empty", "-m", "ahead")
            with mock.patch.dict(os.environ, {"AGENT_DISPATCH_CACHE_ROOT": str(root / "cache")}):
                for setup, passed in [("touch marker", True), ("true", False)]:
                    result = gates.check_base_baseline(repo, "r", repo, base, "test -f marker", root / "run", setup_cmd_override=setup)
                    self.assertEqual(result["passed"], passed)
                    self.assertFalse(result["cached"])
                for verify in ("git commit --allow-empty -m change", "git switch -c other && false"):
                    with self.assertRaisesRegex(RuntimeError, "changed the worktree branch or HEAD"):
                        gates.check_base_baseline(repo, "r", repo, base, verify, root / "run")
                self.assertFalse(list((root / "cache/worktrees/r").glob(".baseline-*")))
                self.assertEqual(_git(repo, "worktree", "list", "--porcelain").count("worktree "), 1)

    def test_reused_branch_baseline_runs_in_detached_worktree_and_catches_red_base(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            bare = d / "origin.git"
            repo = d / "repo"
            _git(d, "init", "--bare", "-b", "main", str(bare))
            _git(d, "clone", str(bare), str(repo))
            _git(repo, "config", "user.email", "t@example.com")
            _git(repo, "config", "user.name", "T")
            # Base commit has a FAILING test!
            (repo / "test_x.py").write_text("import unittest\nclass T(unittest.TestCase):\n    def test_fail(self): self.assertEqual(1, 2)\n", encoding="utf-8")
            _git(repo, "add", "-A")
            _git(repo, "commit", "-m", "red base")
            _git(repo, "push", "-u", "origin", "main")

            with mock.patch.dict(os.environ, {"AGENT_DISPATCH_CACHE_ROOT": str(d / "cache")}):
                wt, branch, base_sha = git_ops.prepare_worktree(repo, "TST-BASE", "main", issue_uuid="uuid-base")
                # Feature branch fixes the test so head != base_sha (reused branch!)
                (wt / "test_x.py").write_text("import unittest\nclass T(unittest.TestCase):\n    def test_ok(self): self.assertEqual(1, 1)\n", encoding="utf-8")
                _git(wt, "add", "-A")
                _git(wt, "commit", "-m", "fix on feature")
                run_dir = d / "run"
                run_dir.mkdir()
                base_res = gates.check_base_baseline(
                    repo, git_ops.repo_cache_key(repo), wt, base_sha, "python3 -m unittest test_x.py", run_dir
                )
            self.assertTrue(base_res["checked"], "Reused feature branch MUST NOT silently skip baseline check!")
            self.assertFalse(base_res["passed"], "Red base_sha MUST fail baseline check in detached worktree!")

    def test_parallel_detached_baselines_share_one_cache_entry(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            # Concurrent checks for the same base share the per-key lock.
            bare = d / "par.git"
            prepo = d / "par"
            _git(d, "init", "--bare", "-b", "main", str(bare))
            _git(d, "clone", str(bare), str(prepo))
            _git(prepo, "config", "user.email", "t@example.com")
            _git(prepo, "config", "user.name", "T")
            (prepo / "test_ok.py").write_text("import unittest\nclass T(unittest.TestCase):\n    def test_ok(self): self.assertEqual(1,1)\n", encoding="utf-8")
            _git(prepo, "add", "-A")
            _git(prepo, "commit", "-m", "init")
            _git(prepo, "push", "-u", "origin", "main")
            with mock.patch.dict(os.environ, {"AGENT_DISPATCH_CACHE_ROOT": str(d / "par_cache")}):
                wt1, _, base_sha = git_ops.prepare_worktree(prepo, "PAR-1", "main", issue_uuid="u1")
                wt2, _, _ = git_ops.prepare_worktree(prepo, "PAR-2", "main", issue_uuid="u2")
                for wt in (wt1, wt2):
                    (wt / "f.txt").write_text("x\n", encoding="utf-8")
                    _git(wt, "add", "f.txt")
                    _git(wt, "commit", "-m", "ahead")
                rkey = git_ops.repo_cache_key(prepo)
                with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                    f1 = pool.submit(gates.check_base_baseline, prepo, rkey, wt1, base_sha, "python3 -m unittest test_ok.py", d / "r1")
                    f2 = pool.submit(gates.check_base_baseline, prepo, rkey, wt2, base_sha, "python3 -m unittest test_ok.py", d / "r2")
                    res1, res2 = f1.result(), f2.result()
                self.assertTrue(res1["passed"] and res2["passed"])
                # One ran baseline, the other hit the exact baseline cache cleanly under per-key lock!
                self.assertEqual(sorted([bool(res1.get("cached")), bool(res2.get("cached"))]), [False, True])

    def test_failed_baseline_leaves_fresh_worktree_clean(self) -> None:
        repo = self.sandbox / "repo"
        _git(self.sandbox, "init", "-b", "main", str(repo))
        _git(repo, "config", "user.email", "test@example.invalid")
        _git(repo, "config", "user.name", "Test")
        (repo / "app.py").write_text("print('base')\n", encoding="utf-8")
        _git(repo, "add", "app.py")
        _git(repo, "commit", "-m", "base")
        base = _git(repo, "rev-parse", "HEAD")
        worktree = self.sandbox / "worktree"
        _git(repo, "worktree", "add", "-b", "fresh", str(worktree), base)
        result = gates.check_base_baseline(
            repo,
            git_ops.repo_cache_key(repo),
            worktree,
            base,
            "echo dirty > app.py && echo junk > temp.out && exit 1",
            self.sandbox / "run-red",
        )
        self.assertFalse(result["passed"])
        self.assertEqual(_git(worktree, "status", "--porcelain"), "")


class TestPolicyGates(IsolatedDispatchCase):
    def test_nested_protected_files_and_literal_test_pathspecs(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _git(root, "init", "-b", "main")
            _git(root, "config", "user.email", "test@example.invalid")
            _git(root, "config", "user.name", "Test")
            _git(root, "commit", "--allow-empty", "-m", "base")
            files = {"packages/a/.cargo/config.toml": "", "packages/a/package.json": '{"scripts":{"test":"true"}}',
                     "packages/a/docs/adr/decision.md": "doc", "tests/test_[fixture].py": "@unittest.skip('reason')"}
            for name, content in files.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)
            _git(root, "add", "-A")
            _git(root, "commit", "-m", "candidate")
            protected = gates.check_protected_paths(root, "HEAD^", "HEAD")
            self.assertTrue(all(any(path in hit for hit in protected) for path in list(files)[:3]))
            self.assertTrue(gates.check_test_integrity_diff(root, "HEAD^", "HEAD"))

    def test_no_renames_catches_git_mv_bypass(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            _git(repo, "init", "-b", "main")
            _git(repo, "config", "user.email", "t@example.com")
            _git(repo, "config", "user.name", "T")
            (repo / "tests" / "unit").mkdir(parents=True)
            (repo / "tests" / "unit" / "test_math.py").write_text("def test_add():\n    assert 1+1==2\n", encoding="utf-8")
            (repo / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
            _git(repo, "add", "-A")
            _git(repo, "commit", "-m", "init")
            base_sha = _git(repo, "rev-parse", "HEAD")

            # Test --no-renames catches git mv of test file & pytest.ini!
            _git(repo, "mv", "tests/unit/test_math.py", "math_checks.py")
            _git(repo, "mv", "pytest.ini", "old_pytest.ini")
            _git(repo, "commit", "-m", "sneaky rename")
            cand_sha = _git(repo, "rev-parse", "HEAD")

            prot = gates.check_protected_paths(repo, base_sha, cand_sha)
            self.assertTrue(any("pytest.ini" in x for x in prot))
            ti = gates.check_test_integrity_diff(repo, base_sha, cand_sha)
            self.assertTrue(any("tests/unit/test_math.py" in x for x in ti))

    def test_non_ascii_paths_utf8_replace_hooks_and_granular_gates(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            repo = d / "repo"
            repo.mkdir()
            _git(repo, "init", "-b", "main")
            _git(repo, "config", "user.email", "t@example.com")
            _git(repo, "config", "user.name", "T")
            (repo / "docs" / "adr").mkdir(parents=True)
            (repo / "docs" / "adr" / "中文.md").write_text("---\nstatus: accepted\n---\n# 中文 ADR\n", encoding="utf-8")
            (repo / "packages" / "api").mkdir(parents=True)
            (repo / "packages" / "api" / "pyproject.toml").write_text("[tool.pytest]\n", encoding="utf-8")
            (repo / "mk").mkdir()
            (repo / "mk" / "test.mk").write_text("test:\n\ttrue\n", encoding="utf-8")
            (repo / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
            (repo / "package.json").write_text('{"scripts": {"test": "vitest"}}', encoding="utf-8")
            (repo / "app").mkdir()
            (repo / "app" / "tests.py").write_text("import unittest\nclass T(unittest.TestCase):\n    def test_ok(self): pass\n", encoding="utf-8")
            (repo / "go_test.go").write_text("package main\nfunc TestA(t *testing.T) {}\n", encoding="utf-8")
            _git(repo, "add", "-A")
            _git(repo, "commit", "-m", "init")
            base_sha = _git(repo, "rev-parse", "HEAD")

            # Install a failing pre-commit hook -> orchestrator_commit MUST disable hooks and succeed!
            hook = repo / ".git" / "hooks" / "pre-commit"
            _write_exec(hook, "#!/bin/sh\necho hook_must_not_run >&2\nexit 1\n")

            # Mutate Chinese ADR, nested pyproject.toml, *.mk, pytest.ini, invalid package.json, delete app/tests.py, add t.SkipNow()
            (repo / "docs" / "adr" / "中文.md").write_text("# modified\n", encoding="utf-8")
            (repo / "packages" / "api" / "pyproject.toml").write_text("# tampered\n", encoding="utf-8")
            (repo / "mk" / "test.mk").write_text("test:\n\techo hacked\n", encoding="utf-8")
            (repo / "pytest.ini").write_text("[pytest]\naddopts = -q\n", encoding="utf-8")
            (repo / "package.json").write_text("{broken json", encoding="utf-8")
            (repo / "app" / "tests.py").unlink()
            (repo / "go_test.go").write_text("package main\nfunc TestA(t *testing.T) { t.SkipNow() }\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {"AGENT_DISPATCH_CACHE_ROOT": str(d / "cache")}):
                cand_sha, sec_hits = gates.orchestrator_commit(repo, "TST-99", "normal title", 1)
            self.assertEqual(sec_hits, [])
            self.assertNotEqual(cand_sha, base_sha)

            # Authorizing ONLY `- gate: pytest.ini` MUST exempt pytest.ini while STILL blocking all other protected files & test skips!
            prot = gates.check_protected_paths(repo, base_sha, cand_sha, allowed_patterns=["pytest.ini"])
            self.assertFalse(any("pytest.ini" in x for x in prot), prot)
            self.assertTrue(any("docs/adr/中文.md" in x for x in prot), prot)
            self.assertTrue(any("packages/api/pyproject.toml" in x for x in prot), prot)
            self.assertTrue(any("mk/test.mk" in x for x in prot), prot)
            self.assertTrue(any("Invalid JSON in `package.json`" in x for x in prot), prot)

            ti = gates.check_test_integrity_diff(repo, base_sha, cand_sha, allowed_patterns=[])
            self.assertTrue(any("app/tests.py" in x for x in ti), ti)
            self.assertTrue(any("SkipNow" in x for x in ti), ti)

            # Non-ASCII secret file `目录/.env` MUST be blocked before staging!
            (repo / "目录").mkdir()
            (repo / "目录" / ".env").write_text("SECRET=1\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {"AGENT_DISPATCH_CACHE_ROOT": str(d / "cache")}):
                _, sec_hits2 = gates.orchestrator_commit(repo, "TST-99", "secret file test", 2)
            self.assertTrue(any("目录/.env" in x for x in sec_hits2), sec_hits2)

    def test_non_object_package_json_and_secret_symlink(self) -> None:
        # package.json containing non-object valid JSON triggers violation cleanly instead of AttributeError;
        # symlink pointing to a secret filename is blocked before git write
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            _git(repo, "init", "-b", "main")
            _git(repo, "config", "user.email", "t@example.invalid")
            _git(repo, "config", "user.name", "Test")
            (repo / "package.json").write_text('{"name":"demo"}\n', encoding="utf-8")
            _git(repo, "add", "package.json")
            _git(repo, "commit", "-m", "init")
            base = _git(repo, "rev-parse", "HEAD")
            (repo / "package.json").write_text("[]\n", encoding="utf-8")
            _git(repo, "add", "package.json")
            _git(repo, "commit", "-m", "array pkg")
            head = _git(repo, "rev-parse", "HEAD")
            viols = gates.check_protected_paths(repo, base, head)
            self.assertTrue(any("Invalid JSON object" in v for v in viols))

            (repo / "link_secret").symlink_to("/etc/app/.env")
            hits = gates.scan_before_git_write(repo)
            self.assertTrue(any("secret symlink target" in h for h in hits))


class TestGitSecretGates(IsolatedDispatchCase):
    def test_raw_http_credentials_are_redacted_before_logging_and_git_write(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / "repo"
            repo.mkdir()
            _git(repo, "init", "-b", "main")
            _git(repo, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                 "commit", "--allow-empty", "-m", "base")
            for header, value in [("Authorization", "token synthetic.opaque.value"),
                                  ("Proxy-Authorization", "Basic syntheticopaque"),
                                  ("X-API-Key", "synthetic.opaque.value"), ("X-Goog-Api-Key", "synthetic_opaque_value")]:
                with self.subTest(header=header):
                    line = f"{header}: {value}\r\n"
                    (repo / "request.txt").write_text(line)
                    self.assertTrue(gates.scan_before_git_write(repo))
                    with contextlib.ExitStack() as stack:
                        log = Path(td) / "request.log"
                        writer = process.RedactedLog(log, stack)
                        writer.write(line.encode())
                    self.assertNotIn(value.split()[-1], log.read_text())

    def test_header_examples_pass_git_gates_but_literal_values_do_not(self) -> None:
        api_key_header = "X-API-Key"
        examples = {
            "client.py": 'headers = {"X-API-Key": API_KEY_VALUE}',
            "client.js": 'const headers = { "X-API-Key": clientKey };',
            "gateway.yaml": "key: &gateway_key ${GATEWAY_KEY}\nheaders:\n"
                            f"  {api_key_header}: *gateway_key # secret-scan:allow",
            "template.yaml": 'headers:\n  X-API-Key: "{{ .Values.gateway.apiKey }}"',
            "README.md": f"- {api_key_header}: Identifies the caller for authentication.",
        }
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            _git(repo, "init", "-b", "main")
            _git(repo, "-c", "user.name=Test", "-c", "user.email=t@example.invalid",
                 "commit", "--allow-empty", "-m", "base")
            for filename, example in examples.items():
                with self.subTest(filename=filename):
                    (repo / filename).write_text(example + "\n")
                    self.assertFalse(gates.scan_before_git_write(repo))
            _git(repo, "add", ".")
            self.assertFalse(gates.scan_staged_for_secrets(repo))
            for literal in ("clientKey", "Identifies", "*gateway_key"):
                (repo / "request.http").write_text(f"{api_key_header}: {literal}\r\n")
                self.assertTrue(gates.scan_before_git_write(repo))

    def test_secret_scan_allow_in_worktree_index_and_history(self) -> None:
        for encoding in ("utf-8", "utf-16-le", "utf-16-be"):
            with self.subTest(encoding=encoding), tempfile.TemporaryDirectory() as td:
                repo = Path(td)
                _git(repo, "init", "-b", "main")
                _git(repo, "config", "user.name", "Test")
                _git(repo, "config", "user.email", "test@example.invalid")
                tracked = repo / "gateway.yaml"
                untracked = repo / "example.yaml"
                declaration = "key: &k ${GATEWAY_KEY}\n"
                tracked.write_bytes(declaration.encode(encoding))
                _git(repo, "add", tracked.name)
                _git(repo, "commit", "-m", "existing placeholder")
                for prefix, value in (
                    ("X-API-Key: ", "*k"),
                    ("Authorization: ", "*k"),
                    ("X-API-Key: ", "synthetic.opaque.value"),
                ):
                    for marker in ("", " # secret-scan:allow"):
                        with self.subTest(header=prefix, value=value, marked=bool(marker)):
                            content = f"{declaration}headers:\n  {prefix}{value}{marker}\n".encode(encoding)
                            untracked.write_bytes(content)
                            self.assertEqual(gates.scan_before_git_write(repo), [] if marker else [
                                "Potential secret token in untracked file content",
                            ])
                            untracked.unlink()
                            tracked.write_bytes(content)
                            expected = [] if marker else ["Potential secret token in added file content"]
                            self.assertEqual(gates.scan_before_git_write(repo), expected)
                            _git(repo, "add", tracked.name)
                            self.assertEqual(gates.scan_staged_for_secrets(repo), expected)
                            _git(repo, "commit", "-m", "change synthetic config")
                            self.assertEqual(gates.scan_range_for_secrets(repo, "HEAD^", "HEAD"), expected)

    def test_secret_scan_allow_preserves_non_bypassable_checks(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            _git(repo, "init", "-b", "main")
            _git(repo, "config", "user.name", "Test")
            _git(repo, "config", "user.email", "test@example.invalid")
            _git(repo, "commit", "--allow-empty", "-m", "base")

            marker = " # secret-scan:allow"
            token = "lin_api_" + "syntheticvalue123456"
            live_value = "synthetic process credential"
            key_kind = "PRIVATE KEY"
            pem = f"-----BEGIN {key_kind}-----{marker}\nsynthetic-body\n-----END {key_kind}-----"
            with mock.patch.dict(os.environ, {"UNIT_API_KEY": live_value}):
                for filename, content, credential in (
                    ("token.txt", f"note{marker} {token}", token),
                    ("key.txt", pem, "synthetic-body"),
                    ("live.txt", f"note{marker} {live_value}", live_value),
                    (".env", f"SETTING=ordinary{marker}", None),
                ):
                    with self.subTest(filename=filename):
                        expected_file = ["Potential secret file: .env"]
                        expected_content = ["Potential secret token in added file content"]
                        (repo / filename).write_text(content + "\n", encoding="utf-8")
                        self.assertEqual(gates.scan_before_git_write(repo), expected_file if credential is None else [
                            "Potential secret token in untracked file content",
                        ])
                        _git(repo, "add", filename)
                        expected = expected_file if credential is None else expected_content
                        self.assertEqual(gates.scan_staged_for_secrets(repo), expected)
                        _git(repo, "commit", "-m", "introduce synthetic fixture")
                        self.assertEqual(gates.scan_range_for_secrets(repo, "HEAD^", "HEAD"), expected)

            _git(repo, "commit", "--allow-empty", "-m", "Bearer " + "synthetic.opaque.value" + marker)
            self.assertEqual(gates.scan_range_for_secrets(repo, "HEAD^", "HEAD"), [
                "Potential secret token in commit message",
            ])

    def test_git_secret_scan_covers_binary_added_text_names_and_intermediate_commits(self) -> None:
        token = "lin_api_" + "SYNTHETICEXAMPLE012345"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _git(root, "init", "-b", "main")
            _git(root, "config", "user.email", "test@example.invalid")
            _git(root, "config", "user.name", "Test")
            _git(root, "commit", "--allow-empty", "-m", "base")
            base = _git(root, "rev-parse", "HEAD")
            for name, content in [("asset.dat", b"\0 " + token.encode()), ("text", ("++ " + token).encode()),
                                  (token, b"ordinary")]:
                with self.subTest(file_kind="name" if name == token else name):
                    path = root / name
                    path.write_bytes(content)
                    _git(root, "add", "--", name)
                    hits = gates.scan_staged_for_secrets(root)
                    self.assertTrue(hits)
                    self.assertNotIn(token, str(hits))
                    _git(root, "commit", "-m", "introduce fixture")
                    head = _git(root, "rev-parse", "HEAD")
                    self.assertTrue(gates.scan_range_for_secrets(root, base, head))
                    path.unlink()
                    _git(root, "commit", "-am", "remove fixture")
                    cleaned = _git(root, "rev-parse", "HEAD")
                    self.assertFalse(gates.scan_range_for_secrets(root, head, cleaned))
                    self.assertTrue(gates.scan_range_for_secrets(root, base, cleaned))
                    base = cleaned

    def test_json_quoted_authorization_credentials_across_git_scans(self) -> None:
        token = "synthetic_pd_tok_987654321"
        # Same synthetic JSON fixture across working tree, staged index, and commit history.
        settings_json = json.dumps(
            {
                "pagerduty": {"headers": {"Authorization": f"Token token={token}"}},
                "proxy": {"headers": {"Proxy-Authorization": f"ApiKey {token}"}},
            },
            indent=2,
        ) + "\n"

        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / "repo"
            repo.mkdir()
            _git(repo, "init", "-b", "main")
            _git(repo, "config", "user.name", "Test")
            _git(repo, "config", "user.email", "test@example.invalid")
            _git(repo, "commit", "--allow-empty", "-m", "base")
            base = _git(repo, "rev-parse", "HEAD")

            cfg = repo / "settings.json"
            cfg.write_text(settings_json, encoding="utf-8")
            self.assertTrue(gates.scan_before_git_write(repo))
            _git(repo, "add", "settings.json")
            self.assertTrue(gates.scan_staged_for_secrets(repo))
            _git(repo, "commit", "-m", "add synthetic json credentials")
            introduced = _git(repo, "rev-parse", "HEAD")
            self.assertTrue(gates.scan_range_for_secrets(repo, base, introduced))

            # A removal passes on its own. The full history still includes the original addition.
            cfg.write_text('{"pagerduty": {"headers": {"Authorization": "Token token=${PAGERDUTY_TOKEN}"}}}\n', encoding="utf-8")
            _git(repo, "commit", "-am", "replace with placeholder")
            cleaned = _git(repo, "rev-parse", "HEAD")
            self.assertFalse(gates.scan_range_for_secrets(repo, introduced, cleaned))
            self.assertTrue(gates.scan_range_for_secrets(repo, base, cleaned))

    def test_utf16_credentials_block_untracked_staged_and_history_but_allow_removal(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            _git(repo, "init", "-b", "main")
            _git(repo, "config", "user.name", "Test")
            _git(repo, "config", "user.email", "test@example.invalid")
            _git(repo, "commit", "--allow-empty", "-m", "base")
            file = repo / "config.txt"
            (repo / ".gitattributes").write_text("config.txt diff\n")
            _git(repo, "add", ".gitattributes")
            _git(repo, "commit", "-m", "show text diff")
            for encoding in ("utf-16-le", "utf-16-be"):
                with self.subTest(encoding=encoding):
                    base = _git(repo, "rev-parse", "HEAD")
                    original = "\ufeff项目\r\nsetting="
                    token = "lin_api_" + "SYNTHETIC" * 3
                    file.write_bytes((original + token + "\r\n结尾").encode(encoding))
                    self.assertTrue(gates.scan_before_git_write(repo))
                    _git(repo, "add", "config.txt")
                    self.assertTrue(gates.scan_staged_for_secrets(repo))
                    _git(repo, "commit", "-m", "introduce synthetic fixture")
                    introduced = _git(repo, "rev-parse", "HEAD")
                    self.assertTrue(gates.scan_range_for_secrets(repo, base, introduced))
                    file.write_bytes((original + "empty\r\n结尾").encode(encoding))
                    _git(repo, "commit", "-am", "remove synthetic fixture")
                    self.assertFalse(gates.scan_range_for_secrets(repo, introduced, "HEAD"))
                    self.assertTrue(gates.scan_range_for_secrets(repo, base, "HEAD"))
                    _git(repo, "rm", "config.txt")
                    _git(repo, "commit", "-m", "remove file")

    def test_commit_rejects_secret_edits_without_writing_git_objects(self) -> None:
        token = "lin_api_" + "SYNTHETICWORKTREEEXAMPLE"
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            _git(repo, "init", "-b", "main")
            _git(repo, "config", "user.email", "test@example.invalid")
            _git(repo, "config", "user.name", "Test")
            tracked = repo / "tracked.txt"
            tracked.write_text("base")
            _git(repo, "add", "-A")
            _git(repo, "commit", "-m", "base")
            for path in (tracked, repo / "new.dat"):
                path.write_text(token)
                digest = _git(repo, "hash-object", str(path))
                index_before = _git(repo, "ls-files", "--stage")
                _, hits = gates.orchestrator_commit(repo, "SYN-1", "local edits", 1)
                self.assertTrue(hits)
                self.assertEqual(_git(repo, "ls-files", "--stage"), index_before)
                self.assertEqual(path.read_text(), token)
                self.assertEqual(_git(repo, "cat-file", "-t", digest, check=False), "")
                if path == tracked:
                    path.write_text("base")
                else:
                    path.unlink()

    def test_commit_excludes_setup_files_even_if_some_are_deleted(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = root / "repo"
            repo.mkdir()
            _git(repo, "init", "-b", "main")
            _git(repo, "config", "user.name", "Test")
            _git(repo, "config", "user.email", "test@example.invalid")
            (repo / "app.py").write_text("x = 1\n", encoding="utf-8")
            _git(repo, "add", "app.py")
            _git(repo, "commit", "-m", "init")

            (repo / "venv_cacert.pem").write_text("bundle\n", encoding="utf-8")
            (repo / "cache.tmp").write_text("v1\n", encoding="utf-8")
            setup_files = {
                "venv_cacert.pem": gates.file_identity(repo / "venv_cacert.pem"),
                "cache.tmp": gates.file_identity(repo / "cache.tmp"),
            }
            (repo / "app.py").write_text("x = 2\n", encoding="utf-8")
            # Simulate verify/worker deleting cache.tmp before review while venv_cacert.pem remains
            (repo / "cache.tmp").unlink()
            cand, hits = gates.orchestrator_commit(repo, "TST-SET", "Work", 1, setup_paths=tuple(setup_files))
            self.assertFalse((repo / "cache.tmp").exists())
            self.assertEqual(hits, [])
            self.assertNotIn("venv_cacert.pem", _git(repo, "ls-tree", "-r", "--name-only", cand).splitlines())
