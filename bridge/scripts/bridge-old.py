import json, os, subprocess, uuid
from mcp.server import MCPServer

mcp = MCPServer("codex-local")
codex_home = os.path.expanduser("~/.local/state/codex-mcp")
os.makedirs(codex_home, exist_ok=True)

def rpc(command, cwd):
    p = subprocess.Popen(
        ["codex", "app-server", "--stdio"],
        cwd="/",
        env={**os.environ, "CODEX_HOME": codex_home},
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )

    def send(x):
        p.stdin.write(json.dumps(x) + "\n")
        p.stdin.flush()

    def recv():
        line = p.stdout.readline()
        if not line:
            raise RuntimeError("codex app-server exited")
        return json.loads(line)

    try:
        send({
            "id": 1,
            "method": "initialize",
            "params": {
                "clientInfo": {"name": "codex-mcp", "version": "0"},
                "capabilities": {"experimentalApi": True},
            },
        })

        while (x := recv()).get("id") != 1:
            pass
        if "error" in x:
            raise RuntimeError(x["error"])

        send({"method": "initialized"})

        handle = uuid.uuid4().hex

        send({
            "id": 2,
            "method": "process/spawn",
            "params": {
                "command": command,
                "processHandle": handle,
                "cwd": cwd,
                "outputBytesCap": None,
                "timeoutMs": None,
            },
        })

        while True:
            x = recv()

            if x.get("id") == 2 and "error" in x:
                raise RuntimeError(x["error"])

            if x.get("method") == "process/exited":
                r = x["params"]
                if r["processHandle"] == handle:
                    return r
    finally:
        p.terminate()
        try:
            p.wait(2)
        except subprocess.TimeoutExpired:
            p.kill()

@mcp.tool()
def exec(command: list[str], cwd: str = "/") -> str:
    """Run argv as the local Linux user without Codex sandbox."""
    return json.dumps(rpc(command, cwd), ensure_ascii=False)

if __name__ == "__main__":
    mcp.run()
