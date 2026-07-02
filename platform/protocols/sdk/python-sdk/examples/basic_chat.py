from onto_agent_sdk import OntoAgentClient


def main() -> None:
    client = OntoAgentClient(
        base_url="http://localhost:5055",
        token="YOUR_TOKEN",
        workspace_id="demo-workspace",
    )

    with client.chat.send(
        session_id="session-123",
        message="请分析这个项目",
    ) as stream:
        for event in stream:
            print(event.kind, event.payload)


if __name__ == "__main__":
    main()
