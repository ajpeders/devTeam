"""Orchestrator agent — decomposes high-level tasks and coordinates dev agents."""

from __future__ import annotations

import json
import time

from agents.base.agent import BaseAgent

_MAX_SIBLING_CONTEXT = 4000


class OrchestratorAgent(BaseAgent):
    """Claims orchestrator tasks, decomposes into dev sub-tasks, monitors children."""

    MONITOR_INTERVAL = 10
    MAX_RETRIES_PER_CHILD = 3

    def handle_task(self, task: dict) -> None:
        task_id = task["id"]
        description = self.input_value(task, "content")
        project_id = task.get("project_id", "")

        dev_pool = self._get_dev_pool()
        self.log.info("decomposing task with %d available devs", len(dev_pool))

        sub_tasks = self._decompose(description, dev_pool)
        self.log.info("decomposed into %d sub-tasks", len(sub_tasks))

        child_ids = []
        for st in sub_tasks:
            child_id = self._create_child_task(project_id, task_id, st)
            if not child_id:
                self.log.error("failed to create sub-task: %s", st.get("description", "")[:60])
                continue
            child_ids.append(child_id)
            self.log.info("created sub-task %s: %s", child_id[:8], st.get("description", "")[:60])

        if not child_ids:
            raise RuntimeError("failed to create any sub-tasks")

        self._monitor_children(task_id, project_id, child_ids)

    def _get_dev_pool(self) -> list[dict]:
        try:
            result = self._api_call("/api/dev/pool_state", {})
            return result.get("slots", [])
        except Exception:
            return []

    def _decompose(self, description: str, dev_pool: list[dict]) -> list[dict]:
        pool_summary = json.dumps(dev_pool, indent=2) if dev_pool else "No dev agents configured yet."
        messages = [
            {"role": "system", "content": (
                "You are a tech lead. Break the task into independent sub-tasks for dev agents. "
                "Each sub-task should be a self-contained unit of work.\n\n"
                'Respond with JSON: {"tasks": [{"description": "...", "params": {}}]}\n\n'
                "Keep sub-tasks small and focused. One feature or file per sub-task."
            )},
            {"role": "user", "content": f"Task: {description}\n\nAvailable dev agents:\n{pool_summary}"},
        ]
        response = self.llm.chat(messages)
        try:
            parsed = json.loads(response)
            return parsed.get("tasks", [])
        except json.JSONDecodeError:
            start = response.find("{")
            end = response.rfind("}") + 1
            if start >= 0 and end > start:
                parsed = json.loads(response[start:end])
                return parsed.get("tasks", [])
            return [{"description": description, "params": {}}]

    def _create_child_task(self, project_id: str, parent_id: str, sub_task: dict) -> str:
        result = self._api_call("/api/task/create_internal", {
            "project_id": project_id, "parent_id": parent_id, "type": "dev",
            "description": sub_task.get("description", ""),
            "params": sub_task.get("params", {}),
        })
        return result.get("task_id", "")

    def _monitor_children(self, task_id: str, project_id: str, child_ids: list[str]):
        retry_counts: dict[str, int] = {cid: 0 for cid in child_ids}
        while not self._stop_event.is_set():
            time.sleep(self.MONITOR_INTERVAL)
            children = self._get_children(task_id)
            if not children:
                continue
            all_done = True
            any_failed = False
            for child in children:
                child_id = child["id"]
                status = child["status"]
                params = child.get("input", {}).get("params", {})

                # Handle escalation: dev is blocked and asking orchestrator for guidance
                if params.get("escalate") and params.get("escalate_question"):
                    self.log.info("child %s escalated, answering via LLM", child_id[:8])
                    answer = self._answer_escalation(params["escalate_question"], children)
                    self._api_call("/api/task/edit_internal", {
                        "task_id": child_id,
                        "params": {
                            "escalate": False,
                            "escalate_response": answer,
                        },
                    })
                    # Keep task in_progress so claim loop picks it up again
                    self._api_call("/api/task/status", {
                        "task_id": child_id,
                        "status": "pending",
                        "message": "escalation answered",
                    })
                    status = "pending"

                if status == "completed":
                    self._relay_context(child, children)
                elif status == "failed":
                    retries = retry_counts.get(child_id, 0)
                    if retries < self.MAX_RETRIES_PER_CHILD:
                        self.log.info("child %s failed (attempt %d), retrying", child_id[:8], retries + 1)
                        self._retry_child(child)
                        retry_counts[child_id] = retries + 1
                        all_done = False
                    else:
                        self.log.error("child %s exceeded max retries", child_id[:8])
                        any_failed = True
                elif status in ("pending", "assigned", "in_progress"):
                    all_done = False
            if all_done and not any_failed:
                self.log.info("all children completed successfully")
                self.update_status(task_id, "completed", message="all sub-tasks completed")
                return
            elif all_done and any_failed:
                self.log.error("some children failed after max retries")
                self.update_status(task_id, "failed", message="sub-task(s) failed after retries")
                return

    def _get_children(self, parent_id: str) -> list[dict]:
        try:
            result = self._api_call("/api/task/list_internal", {"parent_id": parent_id})
            return result.get("tasks", [])
        except Exception:
            return []

    def _relay_context(self, completed_child: dict, all_children: list[dict]):
        detail = ""
        for h in completed_child.get("history", []):
            if h.get("status") == "completed":
                detail = h.get("detail", "")
                break
        if not detail:
            return
        for sibling in all_children:
            if sibling["id"] == completed_child["id"]:
                continue
            if sibling["status"] in ("pending", "assigned", "in_progress"):
                context = f"Context from sibling task: {detail}"
                existing_params = sibling.get("input", {}).get("params", {})
                existing_context = existing_params.get("sibling_context", "")
                if context in existing_context:
                    continue
                new_context = f"{existing_context}\n{context}".strip()
                if len(new_context) > _MAX_SIBLING_CONTEXT:
                    new_context = new_context[-_MAX_SIBLING_CONTEXT:]
                try:
                    self._api_call("/api/task/edit_internal", {
                        "task_id": sibling["id"],
                        "params": {"sibling_context": new_context},
                    })
                except Exception:
                    pass

    def _retry_child(self, failed_child: dict):
        child_id = failed_child["id"]
        detail = ""
        for h in failed_child.get("history", []):
            if h.get("status") == "failed":
                detail = h.get("detail", "")
                break
        params = dict(failed_child.get("input", {}).get("params", {}))
        params["retry_context"] = f"Previous attempt failed: {detail}"
        params["retry_count"] = str(int(params.get("retry_count", "0")) + 1)
        self._api_call("/api/task/status", {
            "task_id": child_id,
            "status": "pending",
            "message": "retrying child task",
        })
        self._api_call("/api/task/edit_internal", {
            "task_id": child_id,
            "params": params,
        })

    def _answer_escalation(self, question: str, siblings: list[dict]) -> str:
        """Use LLM to answer a dev's escalation question based on sibling context."""
        sibling_summary = "\n".join([
            f"- [{s.get('status')}] {s.get('input', {}).get('description', '')}"
            for s in siblings
        ])
        messages = [
            {"role": "system", "content": (
                "You are a tech lead answering a developer's question. "
                "Keep answers short and actionable. If uncertain, say so."
            )},
            {"role": "user", "content": (
                f"Developer question: {question}\n\n"
                f"Other tasks in this batch:\n{sibling_summary}"
            )},
        ]
        try:
            return self.llm.chat(messages)
        except Exception:
            return "Contact the orchestrator operator for guidance."


if __name__ == "__main__":
    agent = OrchestratorAgent()
    agent.run()
