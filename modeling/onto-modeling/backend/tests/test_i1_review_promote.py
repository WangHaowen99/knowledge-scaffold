"""I1 审查修复第 3 条：R-10「已转出」的记录跟着蓝图走——回退、撤回之后蓝图里没有这个问题了，资料地图里回到「待处理」，
再转一次生成新的待答问题（不返回已经不存在的编号）。"""

import subprocess

from app.services.packs import contracts
from tests.test_changesets import main_write
from tests.test_i1_research_view import DRAFT, REPORT, env  # noqa: F401  （夹具按名字注入）


def test_promote_record_follows_blueprint(env):  # noqa: F811
    client, _workspaces, _pid, root, base = env
    main_write(root, "research/20261102-合成补货.md", REPORT, "research: 合成补货")
    blueprint = contracts.default().load("blueprint")
    main_write(root, "research/blueprint.json", blueprint.dumps(blueprint.from_draft(DRAFT)), "research: 合成蓝图")
    before = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True).stdout.strip()
    problem = client.get(f"{base}/materials/map").json()["problems"][2]
    first = client.post(f"{base}/materials/problems/{problem['id']}/promote", json={"as": "ask_me"}).json()
    assert client.get(f"{base}/materials/map").json()["problems"][2]["state"] == "promoted"
    assert client.post(f"{base}/versions/revert", json={"commit": before}).status_code == 200
    questions = {f"bq-{q['id']}" for q in client.get(f"{base}/blueprint").json()["questions"]}
    assert first["question_id"] not in questions
    assert client.get(f"{base}/materials/map").json()["problems"][2]["state"] == "open"
    again = client.post(f"{base}/materials/problems/{problem['id']}/promote", json={"as": "ask_me"}).json()
    questions = {f"bq-{q['id']}" for q in client.get(f"{base}/blueprint").json()["questions"]}
    assert again["question_id"] in questions
    assert again["inbox_item_id"] in [item["id"] for item in client.get(f"{base}/inbox").json()["items"]]
