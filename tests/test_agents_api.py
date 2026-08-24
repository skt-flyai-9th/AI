def test_agent_registry_exposes_current_agents(client, auth_headers):
    response = client.get("/api/v1/agents", headers=auth_headers)
    assert response.status_code == 200

    payload = response.json()
    assert payload["count"] == 2
    agents = {item["id"]: item for item in payload["results"]}

    ranking = agents["challenge-ranking"]
    assert ranking["status"] == "AVAILABLE"
    assert ranking["result_endpoint_template"].endswith("/{run_id}/result")

    shortform = agents["shortform"]
    assert shortform["status"] == "AVAILABLE"
    assert shortform["trigger_endpoint"] == "/api/v1/shortform-sessions"
