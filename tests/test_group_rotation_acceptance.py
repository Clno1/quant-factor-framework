import importlib.util
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from src.alerts.discord import DiscordDeliveryError

spec=importlib.util.spec_from_file_location("accept_rotation",Path(__file__).parents[1]/"scripts/accept_group_rotation.py")
module=importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


@pytest.mark.parametrize("uncertain", [True,False,None])
def test_acceptance_attempt_never_retries(tmp_path,uncertain):
    receipt=tmp_path/"once.json"
    notifier=Mock()
    if uncertain is None:
        notifier.send.return_value={"message_id":"123456"}
        module.deliver_once({"content":"验收","allowed_mentions":{"parse":[]}},notifier,receipt)
        assert json.loads(receipt.read_text())["status"]=="SENT"
    else:
        notifier.send.side_effect=DiscordDeliveryError("sanitized",uncertain=uncertain)
        with pytest.raises(DiscordDeliveryError):
            module.deliver_once({"content":"验收","allowed_mentions":{"parse":[]}},notifier,receipt)
        assert json.loads(receipt.read_text())["status"]==("UNKNOWN" if uncertain else "FAILED")
    with pytest.raises(ValueError,match="already attempted"):
        module.deliver_once({"content":"验收"},notifier,receipt)
    assert notifier.send.call_count==1
