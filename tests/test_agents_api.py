def test_agent_registry_exposes_current_agent(client, auth_headers):
    response = client.get("/api/v1/agents", headers=auth_headers)
    assert response.status_code == 200

    payload = response.json()
    assert payload["count"] == 1
    agent = payload["results"][0]
    assert agent["id"] == "challenge-ranking"
    assert agent["status"] == "AVAILABLE"
    assert agent["result_endpoint_template"].endswith("/{run_id}/result")
