.PHONY: verify test-live

verify:
	bash -n install.sh
	PYTHONDONTWRITEBYTECODE=1 python3 -B -c 'import json; json.loads(open("agy/plugin/plugin.json", encoding="utf-8").read())'
	PYTHONDONTWRITEBYTECODE=1 python3 -B -c 'from pathlib import Path; \
		paths = [Path("bin/agent-dispatch")]; \
		paths.extend(path for root in ("agent_dispatch", "bin", "tests") for path in Path(root).rglob("*.py")); \
		[compile(path.read_bytes(), str(path), "exec") for path in sorted(paths)]'
	PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest discover -s tests

test-live:
	RUN_LIVE_LINEAR_TESTS=1 $(MAKE) verify
