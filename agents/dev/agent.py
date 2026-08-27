"""Dev agent — LLM-driven code generation with sibling coordination."""

from __future__ import annotations

import json
import os

from agents.base.agent import BaseAgent

_MAX_SIBLING_CONTEXT = 4000


class DevAgent(BaseAgent):
    """Takes task descriptions and produces code via LLM."""

    def handle_task(self, task: dict) -> None:
        """Process a dev task: clone repo, create branch, generate code, commit, push."""
        task_id = task["id"]
        repo_url = self.input_value(task, "repo_url")
        content = self.input_value(task, "content")
        parent_id = task.get("parent_id")

        # 1. Clone the repo
        self.log.info("cloning repo %s", repo_url)
        workspace = self.git_clone(task_id, repo_url)

        # 2. Create a feature branch
        branch_name = f"devteam/{task_id[:8]}"
        self.git_branch(workspace, branch_name)
        self.log.info("created branch %s", branch_name)

        # Check for config/task changes before planning
        self._refresh_llm_config()
        task = self._check_task_revision(task)
        content = self.input_value(task, "content")
        params = task.get("input", {}).get("params", {})

        # 3. Gather sibling context and check for escalations
        sibling_context = params.get("sibling_context", "")
        escalate_response = params.get("escalate_response", "")
        escalate = params.get("escalate", False)
        escalate_question = params.get("escalate_question", "")

        siblings = []
        if parent_id:
            siblings = self._get_siblings(task_id, parent_id)
            if siblings:
                self.log.info("found %d sibling tasks", len(siblings))

        # 4. Analyze the task and generate code using LLM
        self.log.info("planning changes via LLM")
        plan = self._plan_changes(
            content,
            workspace,
            sibling_context,
            siblings,
            escalate,
            escalate_question,
            escalate_response,
        )
        file_count = len(plan.get("files", []))
        self.log.info("plan: %d file(s) to create/modify", file_count)

        # Handle escalation: if LLM returned action=escalate, ask orchestrator
        for file_info in plan.get("files", []):
            if file_info.get("action") == "escalate":
                question = file_info.get("description", "blocked")
                self.log.info("escalating to orchestrator: %s", question[:80])
                self._escalate_to_orchestrator(task_id, parent_id, question)
                self.update_status(task_id, "blocked", message=f"escalated: {question[:80]}")
                return

        # Check again before applying
        self._refresh_llm_config()
        task = self._check_task_revision(task)
        content = self.input_value(task, "content")

        self._apply_changes(plan, workspace)

        # 5. Commit and push
        self.git_commit(workspace, f"feat: {content[:72]}")
        self.git_push(workspace, branch_name)
        self.log.info("pushed branch %s", branch_name)

        # 6. Notify siblings of completion
        if parent_id:
            completion_detail = f"Completed: {content[:80]}. Branch: {branch_name}"
            self._notify_siblings(task_id, parent_id, completion_detail)

        # 7. Update task with branch info and trigger review
        self.update_status(task_id, "completed", message=f"branch:{branch_name}")

    def _plan_changes(self, task_description: str, workspace: str,
                      sibling_context: str = "", siblings: list = None,
                      escalate: bool = False, escalate_question: str = "",
                      escalate_response: str = "") -> dict:
        """Use LLM to analyze the task and plan what files to create/modify."""
        file_list = self._get_file_tree(workspace)

        sibling_info = ""
        if siblings:
            sibling_lines = []
            for s in siblings:
                sdesc = s.get("input", {}).get("description", "")
                sstatus = s.get("status", "")
                s_branch = ""
                for h in s.get("history", []):
                    if h.get("status") == "completed":
                        msg = h.get("detail", "")
                        if ":" in msg:
                            s_branch = msg.split(":", 1)[1].strip()
                        break
                sibling_lines.append(f"- [{sstatus}] {sdesc} (branch: {s_branch})")
            sibling_info = "\n\nOther tasks in progress:\n" + "\n".join(sibling_lines)

        context_info = ""
        if sibling_context:
            context_info = f"\n\nContext from completed sibling tasks:\n{sibling_context}"

        escalation_info = ""
        if escalate and escalate_question:
            escalation_info = f"\n\nEscalation to orchestrator required: {escalate_question}"
        elif escalate_response:
            escalation_info = f"\n\nOrchestrator guidance: {escalate_response}"

        messages = [
            {
                "role": "system",
                "content": (
                    "You are a software developer. Analyze the task and plan code changes. "
                    'Respond with JSON: {"files": [{"path": "...", "action": "create|modify", '
                    '"description": "what to do"}]}'
                    "\n\nIf you need a design decision or are blocked, write the question in the "
                    '"description" of the first file and set "action": "escalate".'
                ),
            },
            {
                "role": "user",
                "content": f"Task: {task_description}"
                          f"{sibling_info}"
                          f"{context_info}"
                          f"{escalation_info}"
                          f"\n\nExisting files:\n{file_list}",
            },
        ]

        response = self.llm.chat(messages)

        try:
            return json.loads(response)
        except json.JSONDecodeError:
            # Try to extract JSON from response
            start = response.find("{")
            end = response.rfind("}") + 1
            if start >= 0 and end > start:
                return json.loads(response[start:end])
            return {"files": []}

    def _apply_changes(self, plan: dict, workspace: str) -> None:
        """Use LLM to generate actual code for each planned file change."""
        real_workspace = os.path.realpath(workspace)
        for file_info in plan.get("files", []):
            file_path = os.path.realpath(os.path.join(workspace, file_info["path"]))
            if not file_path.startswith(real_workspace + os.sep):
                self.log.warning("skipping out-of-bounds path: %s", file_info["path"])
                continue

            existing_content = ""
            if os.path.exists(file_path):
                with open(file_path, "r") as f:
                    existing_content = f.read()

            action = file_info.get("action", "create")
            description = file_info.get("description", "")

            if existing_content:
                user_msg = (
                    f"{'Modify' if action == 'modify' else 'Create'} file "
                    f"{file_info['path']}: {description}\n\n"
                    f"Existing content:\n{existing_content}"
                )
            else:
                user_msg = f"Create file {file_info['path']}: {description}"

            messages = [
                {
                    "role": "system",
                    "content": (
                        "You are a software developer. Write the complete file content. "
                        "Output ONLY the code, no markdown fences."
                    ),
                },
                {"role": "user", "content": user_msg},
            ]

            self.log.info("generating code for %s (%s)", file_info["path"], action)
            code = self.llm.chat(messages)

            os.makedirs(os.path.dirname(file_path), exist_ok=True)
            with open(file_path, "w") as f:
                f.write(code)
            self.log.info("wrote %s (%d bytes)", file_info["path"], len(code))

    def _get_siblings(self, task_id: str, parent_id: str) -> list[dict]:
        """Fetch sibling tasks (same parent_id, different id)."""
        try:
            result = self._api_call("/api/task/list_internal", {"parent_id": parent_id})
            return [t for t in result.get("tasks", []) if t.get("id") != task_id]
        except Exception:
            return []

    def _notify_siblings(self, task_id: str, parent_id: str, completion_detail: str) -> None:
        """Send completion context to in-progress siblings via edit_internal."""
        try:
            result = self._api_call("/api/task/list_internal", {"parent_id": parent_id})
            for sibling in result.get("tasks", []):
                if sibling.get("id") == task_id:
                    continue
                if sibling.get("status") in ("pending", "assigned", "in_progress"):
                    existing_params = sibling.get("input", {}).get("params", {})
                    existing_context = existing_params.get("sibling_context", "")
                    new_context = f"{existing_context}\n{completion_detail}".strip()
                    if len(new_context) > _MAX_SIBLING_CONTEXT:
                        new_context = new_context[-_MAX_SIBLING_CONTEXT:]
                    self._api_call("/api/task/edit_internal", {
                        "task_id": sibling["id"],
                        "params": {"sibling_context": new_context},
                    })
        except Exception:
            pass

    def _escalate_to_orchestrator(self, task_id: str, parent_id: str | None, question: str) -> None:
        """Flag the task as needing orchestrator input via sibling_context."""
        if not parent_id:
            return
        try:
            self._api_call("/api/task/edit_internal", {
                "task_id": task_id,
                "params": {"escalate": True, "escalate_question": question},
            })
        except Exception:
            pass

    def _get_file_tree(self, workspace: str) -> str:
        """Get a listing of files in the workspace."""
        files = []
        for root, dirs, filenames in os.walk(workspace):
            # Skip hidden dirs
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for name in filenames:
                rel = os.path.relpath(os.path.join(root, name), workspace)
                files.append(rel)
        return "\n".join(sorted(files))


if __name__ == "__main__":
    agent = DevAgent()
    agent.run()
