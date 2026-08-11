from __future__ import annotations

import sys
import time
from datetime import date
from pathlib import Path

from ib_execution.quote_recorder import RawEventLog, RawTick


root = Path(sys.argv[1])
ready = Path(sys.argv[2])
session = date.fromisoformat(sys.argv[3])
log = RawEventLog(root, session=session, batch_records=1)
log.append(
    RawTick(
        event_id=1,
        recorder_run_id=log.run_id,
        connection_epoch=1,
        contract_id=756733,
        event_type="BID_ASK",
        broker_timestamp=f"{session.isoformat()}T13:30:00+00:00",
        local_wall_ns=1,
        local_monotonic_ns=1,
        market_data_type="LIVE",
        receive_sequence=1,
        bid=600.0,
        ask=600.01,
    ),
    now_mono=1.0,
)
log.flush(publish=False)
ready.write_text("ready", encoding="utf-8")
while True:
    time.sleep(1)
