"""One permission, one name: the project key picker (v3.projects.key.*) must name each
global permission key with the account page's label (v2.users.perm.*).

Regression: the V3 member dialog called criteria.manage 「管理判据」, golden.admit
「准入黄金样本」, judge.calibrate 「认定裁判校准」 and release.sign 「签发放行」 while the
account page and the workshop say 编辑判据 / 准入黄金集 / 判定校准 / 签署放行 for the same keys.
The picker may add a noun ("部署 Agent" for 部署), so the account label must be contained
in the picker label, case-insensitively.
"""

import json
from pathlib import Path

import pytest

LOCALES = Path(__file__).resolve().parents[2] / "frontend" / "src" / "locales"


@pytest.mark.parametrize("lang", ["en", "zh-CN"])
def test_project_key_labels_match_account_labels(lang):
    data = json.loads((LOCALES / lang / "common.json").read_text(encoding="utf-8"))
    account = data["v2"]["users"]["perm"]
    picker = data["v3"]["projects"]["key"]
    shared = sorted(k for k in picker if isinstance(account.get(k), str))
    assert len(shared) >= 16
    wrong = {
        k: (account[k], picker[k])
        for k in shared
        if account[k].lower() not in picker[k].lower()
    }
    assert wrong == {}
