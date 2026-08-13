from __future__ import annotations

from pathlib import Path

path = Path(__file__).with_name("_agent_apply_recovery_p0.py")
text = path.read_text(encoding="utf-8")
old = '''replace_once(
    "src/ib_execution/quote_recorder.py",
    '        self._resubscribe = False\\n',
    '',
)
'''
new = '''replace_once(
    "src/ib_execution/quote_recorder.py",
    '''        self._market_data_type = "UNKNOWN"\\n        self._clock_skew_samples: list[float] = []\\n        self._resubscribe = False\\n        self._fatal_prerequisite_error: Optional[str] = None\\n''',
    '''        self._market_data_type = "UNKNOWN"\\n        self._clock_skew_samples: list[float] = []\\n        self._fatal_prerequisite_error: Optional[str] = None\\n''',
)
'''
if text.count(old) != 1:
    raise RuntimeError(f"expected one generic _resubscribe patch block, got {text.count(old)}")
text = text.replace(old, new, 1)
code = compile(text, str(path), "exec")
exec(code, {"__file__": str(path), "__name__": "__main__"})
