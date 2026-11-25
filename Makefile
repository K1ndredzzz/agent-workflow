.PHONY: verify test-live

verify:
	bash -n install.sh
	PYTHONDONTWRITEBYTECODE=1 python3 -B -c 'import json; json.loads(open("agy/plugin/plugin.json", encoding="utf-8").read()); [compile(open(p, encoding="utf-8").read(), p, "exec") for p in ("bin/agent-dispatch", "tests/test_agent_dispatch.py")]'
	PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest discover -s tests

test-live:
	RUN_LIVE_LINEAR_TESTS=1 $(MAKE) verify
