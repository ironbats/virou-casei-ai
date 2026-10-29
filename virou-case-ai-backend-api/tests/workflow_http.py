def reviewed_message(client, headers, project, key, brief, reply_to=None):
    requests = getattr(client, "reviewed_requests", {})
    if key not in requests:
        base = f"/api/v1/projects/{project['id']}"
        draft = client.get(base + "/build-draft").json()
        response = client.put(
            base + "/build-draft",
            json={
                **draft["data"],
                "stage": "idea",
                "message_text": brief,
                "attachments": [{**item, "enabled": False} for item in draft["data"]["attachments"]],
            },
            headers={**headers, "Idempotency-Key": key + ":idea", "If-Match": str(draft["version"])},
        )
        assert response.status_code == 200, response.text
        response = client.post(
            base + "/recommendations",
            json={
                "expected_draft_revision": response.json()["version"],
                "base_project_revision": project["current_revision_id"],
            },
            headers={**headers, "Idempotency-Key": key + ":prepare"},
        )
        assert response.status_code == 200, response.text
        draft = client.get(base + "/build-draft").json()
        response = client.post(
            base + "/build-review",
            json={
                "expected_draft_revision": draft["version"],
                "confirm_empty": True,
            },
            headers={**headers, "Idempotency-Key": key + ":review"},
        )
        assert response.status_code == 200, response.text
        reviewed = response.json()
        requests[key] = {
            "expected_draft_revision": reviewed["version"],
            "base_project_revision": project["current_revision_id"],
            "review_snapshot_hash": reviewed["data"]["review_snapshot"]["hash"],
            "policy_version": "2026-10-v1",
            "reply_to": reply_to,
        }
        client.reviewed_requests = requests
    return client.post(
        f"/api/v1/projects/{project['id']}/messages",
        json=requests[key],
        headers={**headers, "Idempotency-Key": key},
    )
