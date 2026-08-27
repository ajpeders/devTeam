"""QA agent for running tests and generating new tests."""

from __future__ import annotations

import json
import os
import subprocess
import sys

from agents.base.agent import BaseAgent


class QAAgent(BaseAgent):
    """Agent that runs tests and generates new tests for code changes."""

    def handle_task(self, task: dict) -> None:
        """Run tests and generate new tests for code changes."""
        task_id = task["id"]
        repo_url = self.input_value(task, "repo_url")
        branch = self.input_value(task, "branch")
        content = self.input_value(task, "content")

        # 1. Clone and checkout branch
        workspace = self.git_clone(task_id, repo_url)
        self.log.info("checking out branch %s", branch)
        subprocess.run(
            ["git", "checkout", branch],
            cwd=workspace,
            capture_output=True,
            check=True,
        )

        # 2. Detect test runner
        runner = self._detect_test_runner(workspace)
        self.log.info("detected test runner: %s", runner)

        # 3. Run existing tests
        existing_results = self._run_tests(workspace, runner)
        if existing_results.get("passed"):
            self.log.info("existing tests passed")
        else:
            self.log.warning("existing tests failed")

        # 4. Get the diff to understand what changed
        diff = self._get_diff(workspace, branch)

        # 5. Generate new tests using LLM
        new_tests = self._generate_tests(diff, content, workspace, runner)

        # 6. Write new test files (syntax-validated), retry once if all were invalid
        self.log.info("generated %d new test files", len(new_tests))
        wrote_any = False
        for attempt in range(2):
            if attempt > 0:
                # Previous attempt wrote nothing valid — regenerate
                self.log.warning("all test files invalid, regenerating (attempt %d)", attempt + 1)
                new_tests = self._generate_tests(diff, content, workspace, runner)
                self.log.info("regenerated %d new test files", len(new_tests))
            wrote_any = self._write_tests(new_tests, workspace)
            if wrote_any:
                break

        if not wrote_any:
            self.log.warning("no valid test files generated after 2 attempts")
            self.update_status(task_id, "failed", message="tests_failed|no valid test files generated")
            return

        # 7. Run all tests again (including new ones)
        final_results = self._run_tests(workspace, runner)

        # 8. Commit and push new tests
        self.git_commit(workspace, f"test: add tests for {content[:50]}")
        self.git_push(workspace, branch)

        # 9. Report results
        passed = final_results.get("passed", False)
        if passed:
            self.log.info("final result: tests passed")
            self.update_status(
                task_id,
                "completed",
                message=f"tests_passed|{final_results.get('summary', '')}",
            )
        else:
            self.log.warning("final result: tests failed")
            self.update_status(
                task_id,
                "failed",
                message=f"tests_failed|{final_results.get('summary', '')}",
            )

    def _detect_test_runner(self, workspace: str) -> str:
        """Detect which test runner to use based on project files."""
        if os.path.exists(os.path.join(workspace, "pytest.ini")) or os.path.exists(
            os.path.join(workspace, "pyproject.toml")
        ):
            return "pytest"
        if os.path.exists(os.path.join(workspace, "go.mod")):
            return "go"
        if os.path.exists(os.path.join(workspace, "package.json")):
            return "npm"
        if os.path.exists(os.path.join(workspace, "Cargo.toml")):
            return "cargo"
        return "pytest"  # default

    def _run_tests(self, workspace: str, runner: str) -> dict:
        """Run the test suite and return results."""
        commands = {
            "pytest": [sys.executable, "-m", "pytest", "-v", "--tb=short"],
            "go": ["go", "test", "./...", "-v"],
            "npm": ["npm", "test"],
            "cargo": ["cargo", "test"],
        }
        cmd = commands.get(runner, [sys.executable, "-m", "pytest", "-v"])
        self.log.info("running tests: %s", " ".join(cmd))

        try:
            result = subprocess.run(
                cmd, cwd=workspace, capture_output=True, text=True, timeout=300
            )
            rc = result.returncode
            # pytest rc=5 means "no tests collected" — not a failure, just means
            # there are no existing tests to run, so we should proceed to generate.
            passed = rc == 0 or (runner == "pytest" and rc == 5)
            if passed:
                if rc == 5:
                    self.log.info("no existing tests collected (rc=5), proceeding to generate")
                else:
                    self.log.info("tests passed (rc=%d)", rc)
            else:
                self.log.warning("tests failed (rc=%d)", rc)
            output = result.stdout + result.stderr
            return {
                "passed": passed,
                "summary": output[-500:] if len(output) > 500 else output,
                "returncode": rc,
            }
        except subprocess.TimeoutExpired:
            self.log.warning("test execution timed out")
            return {
                "passed": False,
                "summary": "Test execution timed out",
                "returncode": -1,
            }

    def _get_diff(self, workspace: str, branch: str) -> str:
        """Get diff of changes on the branch."""
        result = subprocess.run(
            ["git", "diff", "HEAD~1", "--", "."],
            cwd=workspace,
            capture_output=True,
            text=True,
        )
        return result.stdout

    def _generate_tests(
        self,
        diff: str,
        task_description: str,
        workspace: str,
        runner: str,
    ) -> list[dict]:
        """Use LLM to generate tests for changed code."""
        if not self.llm:
            self.log.warning("no LLM configured, skipping test generation")
            return []

        self.log.info("generating tests via LLM")
        messages = [
            {
                "role": "system",
                "content": (
                    f"You are a QA engineer. The project uses {runner} for testing. "
                    "Generate tests for the changed code. "
                    'Respond with JSON: {"test_files": [{"path": "...", "content": "..."}]}'
                ),
            },
            {
                "role": "user",
                "content": f"Task: {task_description}\n\nChanges:\n{diff}",
            },
        ]

        response = self.llm.chat(messages)
        try:
            parsed = json.loads(response)
            files = parsed.get("test_files", [])
            self.log.info("LLM returned %d test files", len(files))
            return files
        except json.JSONDecodeError:
            # Try to extract JSON from response
            start = response.find("{")
            end = response.rfind("}") + 1
            if start >= 0 and end > start:
                try:
                    parsed = json.loads(response[start:end])
                    files = parsed.get("test_files", [])
                    self.log.info("LLM returned %d test files", len(files))
                    return files
                except json.JSONDecodeError:
                    pass
            # LLM may have returned raw Python code instead of JSON.
            # If response looks like Python, wrap it as a single test file.
            if "def test_" in response or "async def test_" in response:
                # Find a code block or the whole response
                import re
                match = re.search(r"```(?:\w+)?\n(.*?)```", response, re.DOTALL)
                code = match.group(1).strip() if match else response.strip()
                if "def test_" in code:
                    self.log.info("LLM returned raw Python, wrapping as single test file")
                    return [{"path": "tests/test_generated.py", "content": code}]
            self.log.warning("failed to parse LLM response as JSON: %.200s", response[:200])
            return []

    def _write_tests(self, test_files: list[dict], workspace: str) -> bool:
        """Write generated test files to the workspace. Skips files with invalid Python syntax. Returns True if any file was written."""
        import ast
        wrote_any = False
        for test_file in test_files:
            path = os.path.join(workspace, test_file["path"])
            content = test_file["content"]
            try:
                ast.parse(content)
            except SyntaxError as exc:
                self.log.warning(
                    "skipping %s: invalid Python syntax at line %d: %s",
                    test_file["path"], exc.lineno or 0, exc.msg,
                )
                continue
            self.log.info("writing test file: %s", test_file["path"])
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                f.write(content)
            wrote_any = True
        return wrote_any


if __name__ == "__main__":
    agent = QAAgent()
    agent.run()
