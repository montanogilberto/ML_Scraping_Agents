from agents.ml_inventory.agent import root_agent

if __name__ == "__main__":
    import sys, json
    from agents.ml_inventory.runner import run_once
    data = sys.stdin.read().strip()
    if not data:
        print("Provide JSON input via stdin.")
        raise SystemExit(1)
    req = json.loads(data)
    out = run_once(req)
    print(json.dumps(out, ensure_ascii=False, indent=2))
