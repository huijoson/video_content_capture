"""Drive an isolated local video workspace for verification.

Linux only: process and port ownership are read from /proc.

The process this file starts is the only process it will stop. Identity is the
library directory and the Chrome profile created for the run, not a process name.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import shutil
import signal
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

import websockets

from video_content_capture import __version__

SKILL_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[4]
PAGE_TITLE = "影片工作區"
DROPPED_ENV = {
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "ASSEMBLYAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "VCC_LIBRARY_DIR",
    "VCC_HOST",
    "VCC_PORT",
    "VCC_GEMINI_TRANSLATION_MODEL",
    "VCC_GEMINI_QA_MODEL",
    "VCC_ENABLE_LIVE",
}
FIND_JS = r"""
(spec) => {
  const roleOf = (el) => {
    const explicit = el.getAttribute("role");
    if (explicit) return explicit;
    const tag = el.tagName;
    if (tag === "BUTTON") return "button";
    if (tag === "A" && el.hasAttribute("href")) return "link";
    if (tag === "TEXTAREA") return "textbox";
    if (tag === "SELECT") return "combobox";
    if (tag === "INPUT") {
      const type = (el.getAttribute("type") || "text").toLowerCase();
      if (type === "search") return "searchbox";
      if (type === "checkbox") return "checkbox";
      return "textbox";
    }
    if (/^H[1-6]$/.test(tag)) return "heading";
    return "";
  };
  const nameOf = (el) => {
    if (el.getAttribute("aria-label")) return el.getAttribute("aria-label");
    const labelledby = el.getAttribute("aria-labelledby");
    if (labelledby) {
      return labelledby.split(/\s+/).map((id) => {
        const node = document.getElementById(id);
        return node ? node.innerText : "";
      }).join(" ");
    }
    if (el.labels && el.labels.length) {
      return [...el.labels].map((label) => label.innerText).join(" ");
    }
    return el.innerText || el.textContent || "";
  };
  const seen = [];
  for (const el of document.querySelectorAll("body *")) {
    const role = roleOf(el);
    if (role !== spec.role) continue;
    const name = nameOf(el).replace(/\s+/g, " ").trim();
    const visible = el.checkVisibility({visibilityProperty: true});
    if (!visible) continue;
    seen.push(name);
    if (name === spec.name) {
      el.scrollIntoView({block: "center", inline: "nearest"});
      if (spec.op === "click") el.click();
      if (spec.op === "fill") {
        el.focus();
        el.value = spec.value;
        el.dispatchEvent(new Event("input", {bubbles: true}));
        el.dispatchEvent(new Event("change", {bubbles: true}));
      }
      return {ok: true, value: spec.op === "fill" ? el.value : name};
    }
  }
  return {ok: false, seen: seen.slice(0, 20)};
}
"""


def run_home() -> Path:
    override = os.environ.get("VCC_VERIFY_HOME")
    return Path(override).resolve() if override else SKILL_DIR / "run"


def evidence_root() -> Path:
    override = os.environ.get("VCC_VERIFY_EVIDENCE")
    return Path(override).resolve() if override else SKILL_DIR / "evidence"


def state_path() -> Path:
    return run_home() / "state.json"


def load_state() -> dict:
    path = state_path()
    if not path.is_file():
        raise SystemExit("no verification instance; run control-vcc launch")
    return json.loads(path.read_text())


def save_state(state: dict) -> None:
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2) + "\n")


def cmdline(pid: int) -> str:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return ""
    return raw.replace(b"\0", b" ").decode(errors="replace")


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return Path(f"/proc/{pid}").exists()


def descendants(pid: int) -> list[int]:
    found: list[int] = []
    path = Path(f"/proc/{pid}/task/{pid}/children")
    try:
        text = path.read_text()
    except OSError:
        return found
    for item in text.split():
        child = int(item)
        found.append(child)
        found.extend(descendants(child))
    return found


def listening_pids(port: int) -> set[int]:
    hex_port = f"{port:04X}"
    inodes: set[str] = set()
    for table in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            lines = Path(table).read_text().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            parts = line.split()
            local_port = parts[1].rsplit(":", 1)[-1].upper()
            if local_port == hex_port and parts[3] == "0A":
                inodes.add(parts[9])
    if not inodes:
        return set()
    owners: set[int] = set()
    proc = Path("/proc")
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        fd_dir = entry / "fd"
        try:
            fds = list(fd_dir.iterdir())
        except OSError:
            continue
        for fd in fds:
            try:
                target = os.readlink(fd)
            except OSError:
                continue
            if target.startswith("socket:[") and target[8:-1] in inodes:
                owners.add(int(entry.name))
                break
    return owners


def serve_processes(state: dict) -> list[int]:
    marker = state["library_dir"]
    root = int(state["pid"])
    candidates = [root, *descendants(root)]
    return [
        pid
        for pid in candidates
        if pid_alive(pid) and marker in cmdline(pid) and "serve" in cmdline(pid)
    ]


def chrome_processes(state: dict) -> list[int]:
    profile = state.get("chrome_profile")
    if not profile:
        return []
    root = int(state.get("chrome_pid") or 0)
    candidates = [root, *descendants(root)] if root else []
    matched = [pid for pid in candidates if pid_alive(pid) and profile in cmdline(pid)]
    return matched


def stop_pids(pids: list[int]) -> None:
    living = [pid for pid in pids if pid_alive(pid)]
    for pid in living:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and any(pid_alive(pid) for pid in living):
        time.sleep(0.1)
    for pid in living:
        if pid_alive(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def clean_env() -> dict[str, str]:
    return {key: value for key, value in os.environ.items() if key not in DROPPED_ENV}


def http_json(url: str, timeout: float = 2) -> tuple[int, object]:
    request = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
            status = response.status
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode(errors="replace")
    return status, json.loads(body)


def http_text(url: str, timeout: float = 2) -> tuple[int, str]:
    request = urllib.request.Request(url)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.status, response.read().decode(errors="replace")


def command_launch() -> None:
    home = run_home()
    if state_path().is_file():
        existing = json.loads(state_path().read_text())
        if serve_processes(existing):
            raise SystemExit(
                f"verification instance already running at {existing['url']}; "
                "run control-vcc cleanup before launch"
            )
        stop_pids(chrome_processes(existing))
    project = home / "project"
    library = home / "library"
    if project.exists():
        shutil.rmtree(project)
    if library.exists():
        shutil.rmtree(library)
    project.mkdir(parents=True)
    library.mkdir(parents=True)
    dotenv = os.environ.get("VCC_VERIFY_DOTENV")
    if dotenv:
        shutil.copyfile(dotenv, project / ".env")
    port = free_port()
    log_path = home / "serve.log"
    log_handle = log_path.open("w")
    process = subprocess.Popen(
        [
            "uv",
            "run",
            "vcc",
            "serve",
            "--project-dir",
            str(project),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--library-dir",
            str(library),
        ],
        cwd=REPO_ROOT,
        env=clean_env(),
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 20
    last_error = "server did not answer"
    while time.monotonic() < deadline:
        if process.poll() is not None:
            last_error = f"server exited {process.returncode}; see {log_path}"
            break
        try:
            status, payload = http_json(f"{url}/api/status")
            page_status, page = http_text(f"{url}/")
            videos_status, videos = http_json(f"{url}/api/videos")
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = str(exc)
            time.sleep(0.2)
            continue
        sqlite = library / "library.sqlite3"
        if (
            status == 200
            and page_status == 200
            and PAGE_TITLE in page
            and videos_status == 200
            and isinstance(videos, list)
            and sqlite.is_file()
            and isinstance(payload, dict)
            and payload.get("version")
        ):
            state = {
                "pid": process.pid,
                "port": port,
                "url": url,
                "library_dir": str(library),
                "project_dir": str(project),
                "log_path": str(log_path),
                "version": payload["version"],
                "chrome_pid": None,
                "chrome_port": None,
                "chrome_profile": None,
                "page_id": None,
            }
            save_state(state)
            print(f"url={url}")
            print(f"pid={process.pid}")
            print(f"library_dir={library}")
            print(f"version={payload['version']}")
            return
        last_error = f"server answered but {PAGE_TITLE!r} or the library was not ready"
        time.sleep(0.2)
    stop_pids([process.pid, *descendants(process.pid)])
    raise SystemExit(last_error)


def doctor_problems(state: dict) -> list[str]:
    problems: list[str] = []
    owners = serve_processes(state)
    if not owners:
        problems.append("serve process is not the instance this run started")
    listeners = listening_pids(int(state["port"]))
    if not listeners:
        problems.append(f"nothing is listening on {state['port']}")
    elif not any(state["library_dir"] in cmdline(pid) for pid in listeners):
        problems.append(f"port {state['port']} is not owned by this run's library")
    try:
        status, payload = http_json(f"{state['url']}/api/status")
        page_status, page = http_text(f"{state['url']}/")
        videos_status, videos = http_json(f"{state['url']}/api/videos")
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        problems.append(str(exc))
        return problems
    if status != 200 or not isinstance(payload, dict):
        problems.append("status endpoint did not return the service record")
    else:
        if payload.get("version") != __version__:
            problems.append(
                f"running version {payload.get('version')} does not match checkout {__version__}"
            )
    if page_status != 200 or PAGE_TITLE not in page:
        problems.append("workspace page is not being served")
    if videos_status != 200 or not isinstance(videos, list):
        problems.append("video list is not readable")
    sqlite = Path(state["library_dir"]) / "library.sqlite3"
    if not sqlite.is_file():
        problems.append("library database was not initialized")
    return problems


def command_doctor() -> None:
    state = load_state()
    problems = doctor_problems(state)
    if problems:
        raise SystemExit("doctor failed: " + "; ".join(problems))
    status, payload = http_json(f"{state['url']}/api/status")
    assert status == 200 and isinstance(payload, dict)
    gemini = payload["gemini"]["configured"]
    print("ok")
    print(f"url={state['url']}")
    print(f"pid={state['pid']}")
    print(f"port={state['port']}")
    print(f"version={payload['version']}")
    print(f"gemini_configured={str(gemini).lower()}")
    print(f"translation_model={payload['gemini']['translation_model']}")
    print(f"qa_model={payload['gemini']['qa_model']}")
    print(f"library_dir={state['library_dir']}")


def command_read(path: str) -> None:
    if not path.startswith("/api/") or ".." in path:
        raise SystemExit("read only allows a /api/ path on this instance")
    state = load_state()
    problems = doctor_problems(state)
    if problems:
        raise SystemExit("doctor failed: " + "; ".join(problems))
    status, payload = http_json(state["url"] + path)
    print(f"status={status}")
    print(json.dumps(payload, ensure_ascii=False))


def chrome_bin() -> str:
    for candidate in (
        os.environ.get("VCC_VERIFY_CHROME"),
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        shutil.which("google-chrome"),
    ):
        if candidate and Path(candidate).is_file():
            return candidate
    raise SystemExit("google-chrome is not installed")


def ensure_chrome(state: dict) -> dict:
    if state.get("chrome_pid") and chrome_processes(state) and state.get("chrome_port"):
        return state
    profile = run_home() / "chrome"
    if profile.exists():
        shutil.rmtree(profile)
    profile.mkdir(parents=True)
    log_handle = (run_home() / "chrome.log").open("w")
    # The sandbox cannot start inside this container. The profile is one this run created.
    process = subprocess.Popen(
        [
            chrome_bin(),
            "--headless=new",
            "--disable-gpu",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--no-first-run",
            "--no-default-browser-check",
            "--remote-debugging-port=0",
            "--remote-allow-origins=*",
            f"--user-data-dir={profile}",
            "--window-size=1280,900",
            "about:blank",
        ],
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    port_file = profile / "DevToolsActivePort"
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and not port_file.is_file():
        if process.poll() is not None:
            raise SystemExit(f"chrome exited {process.returncode}; see {run_home() / 'chrome.log'}")
        time.sleep(0.1)
    if not port_file.is_file():
        stop_pids([process.pid, *descendants(process.pid)])
        raise SystemExit("chrome did not open a debugging port")
    state["chrome_pid"] = process.pid
    state["chrome_port"] = int(port_file.read_text().splitlines()[0])
    state["chrome_profile"] = str(profile)
    state["page_id"] = None
    save_state(state)
    return state


def debugger_json(port: int, path: str, method: str = "GET") -> object:
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method)
    with urllib.request.urlopen(request, timeout=5) as response:
        raw = response.read()
    return json.loads(raw) if raw else None


def page_target(state: dict) -> dict:
    pages = debugger_json(int(state["chrome_port"]), "/json/list")
    assert isinstance(pages, list)
    chosen = None
    for page in pages:
        if page.get("type") == "page" and page.get("id") == state.get("page_id"):
            chosen = page
            break
    if chosen is None:
        for page in pages:
            if page.get("type") == "page":
                chosen = page
                break
    if chosen is None:
        created = debugger_json(int(state["chrome_port"]), "/json/new?about:blank", method="PUT")
        if not isinstance(created, dict):
            raise SystemExit("chrome did not open a page")
        chosen = created
    state["page_id"] = chosen["id"]
    save_state(state)
    return chosen


class Cdp:
    def __init__(self, socket: websockets.ClientConnection) -> None:
        self.socket = socket
        self.next_id = 0

    async def call(self, method: str, params: dict | None = None, timeout: float = 30) -> dict:
        self.next_id += 1
        ident = self.next_id
        await self.socket.send(json.dumps({"id": ident, "method": method, "params": params or {}}))
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(method)
            raw = await asyncio.wait_for(self.socket.recv(), timeout=remaining)
            message = json.loads(raw)
            if message.get("id") != ident:
                continue
            if "error" in message:
                raise RuntimeError(str(message["error"]))
            result = message.get("result", {})
            assert isinstance(result, dict)
            return result


async def evaluate(cdp: Cdp, expression: str) -> object:
    result = await cdp.call(
        "Runtime.evaluate",
        {"expression": expression, "returnByValue": True, "awaitPromise": True},
    )
    if result.get("exceptionDetails"):
        raise SystemExit(f"page script failed: {result['exceptionDetails']}")
    return result["result"].get("value")


async def with_page(state: dict, action):
    target = page_target(state)
    async with websockets.connect(target["webSocketDebuggerUrl"], max_size=50_000_000) as socket:
        cdp = Cdp(socket)
        await cdp.call("Runtime.enable")
        await cdp.call("Page.enable")
        return await action(cdp)


def command_browser(args: argparse.Namespace) -> None:
    state = load_state()
    problems = doctor_problems(state)
    if problems:
        raise SystemExit("doctor failed: " + "; ".join(problems))
    state = ensure_chrome(state)
    asyncio.run(browser_action(state, args))


async def browser_action(state: dict, args: argparse.Namespace) -> None:
    if args.bcmd == "open":
        await open_page(state)
        return
    if args.bcmd == "click":
        await act(state, "click", args.role, args.name, None)
        return
    if args.bcmd == "fill":
        await act(state, "fill", args.role, args.name, args.value)
        return
    if args.bcmd == "text":
        print(await element_text(state, args.id))
        return
    if args.bcmd == "visible":
        visible = await element_visible(state, args.id)
        print("visible" if visible else "hidden")
        if not visible:
            raise SystemExit(1)
        return
    if args.bcmd == "wait":
        await wait_for_text(state, args)
        return
    if args.bcmd == "snapshot":
        await write_snapshot(state, evidence_file(args.path))
        return
    if args.bcmd == "screenshot":
        await write_screenshot(state, evidence_file(args.path))
        return
    raise SystemExit(f"unknown browser command {args.bcmd}")


def evidence_file(path: str) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = evidence_root() / candidate
    resolved = candidate.resolve()
    root = evidence_root().resolve()
    if root != resolved and root not in resolved.parents:
        raise SystemExit(f"evidence path must stay under {root}")
    return resolved


async def open_page(state: dict) -> None:
    async def action(cdp: Cdp) -> None:
        await cdp.call("Page.navigate", {"url": state["url"]})
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            value = await evaluate(
                cdp,
                """(() => ({
                  title: document.title,
                  service: document.getElementById('service-status')?.innerText || '',
                  library: document.getElementById('library-status')?.innerText || ''
                }))()""",
            )
            assert isinstance(value, dict)
            if (
                value.get("title") == PAGE_TITLE
                and value.get("service")
                and "正在讀取服務狀態" not in value["service"]
                and value.get("library")
                and "正在讀取影片庫" not in value["library"]
            ):
                print(f"url={state['url']}")
                print(f"service={value['service']}")
                print(f"library={value['library']}")
                return
            await asyncio.sleep(0.2)
        raise SystemExit("workspace page did not finish loading")

    await with_page(state, action)


async def act(state: dict, op: str, role: str, name: str, value: str | None) -> None:
    spec = json.dumps({"op": op, "role": role, "name": name, "value": value}, ensure_ascii=False)

    async def action(cdp: Cdp) -> None:
        found = await evaluate(cdp, "(" + FIND_JS + ")(" + spec + ")")
        if not isinstance(found, dict) or not found.get("ok"):
            seen = found.get("seen") if isinstance(found, dict) else []
            raise SystemExit(f"no visible {role} named {name!r}; saw {seen}")
        print(f"{op} {role} {name}")

    await with_page(state, action)


async def element_text(state: dict, element_id: str) -> str:
    async def action(cdp: Cdp) -> str:
        payload = json.dumps(element_id)
        value = await evaluate(
            cdp,
            f"""(() => {{
              const el = document.getElementById({payload});
              if (!el) return null;
              return el.innerText;
            }})()""",
        )
        if value is None:
            raise SystemExit(f"no element #{element_id}")
        return str(value)

    return await with_page(state, action)


async def element_visible(state: dict, element_id: str) -> bool:
    async def action(cdp: Cdp) -> bool:
        payload = json.dumps(element_id)
        value = await evaluate(
            cdp,
            f"""(() => {{
              const el = document.getElementById({payload});
              if (!el) return null;
              return el.checkVisibility({{visibilityProperty: true}});
            }})()""",
        )
        if value is None:
            raise SystemExit(f"no element #{element_id}")
        return bool(value)

    return await with_page(state, action)


async def wait_for_text(state: dict, args: argparse.Namespace) -> None:
    expected = [args.text] if args.text else list(args.any or [])
    if not expected:
        raise SystemExit("wait needs --text or --any")
    deadline = time.monotonic() + args.timeout
    last = ""
    while time.monotonic() < deadline:
        last = await element_text(state, args.id)
        if any(item in last for item in expected):
            print(last)
            return
        await asyncio.sleep(0.2)
    raise SystemExit(f"#{args.id} stayed {last!r}; wanted one of {expected}")


async def write_snapshot(state: dict, path: Path) -> None:
    async def action(cdp: Cdp) -> None:
        await cdp.call("Accessibility.enable")
        tree = await cdp.call("Accessibility.getFullAXTree")
        nodes = tree.get("nodes", [])
        by_id = {node["nodeId"]: node for node in nodes}
        children: dict[str, list[str]] = {node["nodeId"]: [] for node in nodes}
        roots: list[str] = []
        for node in nodes:
            parent = node.get("parentId")
            if parent in by_id:
                children[parent].append(node["nodeId"])
            else:
                roots.append(node["nodeId"])
        lines = [f"url: {state['url']}", f"title: {PAGE_TITLE}", ""]

        def walk(node_id: str, depth: int) -> None:
            node = by_id[node_id]
            role = (node.get("role") or {}).get("value", "")
            name = (node.get("name") or {}).get("value", "")
            skipped = {"generic", "none", "InlineTextBox", "LineBreak"}
            if role == "StaticText" and name:
                lines.append(f"{'  ' * depth}{name}")
            elif role and role not in skipped:
                label = f"{role} {name!r}" if name else role
                lines.append(f"{'  ' * depth}{label}")
            for child_id in children.get(node_id, []):
                walk(child_id, depth + 1)

        for root in roots:
            walk(root, 0)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n")
        print(path)

    await with_page(state, action)


async def write_screenshot(state: dict, path: Path) -> None:
    async def action(cdp: Cdp) -> None:
        await evaluate(cdp, "window.scrollTo(0, 0)")
        metrics = await cdp.call("Page.getLayoutMetrics")
        content = metrics["cssContentSize"]
        shot = await cdp.call(
            "Page.captureScreenshot",
            {
                "format": "png",
                "captureBeyondViewport": True,
                "clip": {
                    "x": 0,
                    "y": 0,
                    "width": float(content["width"]),
                    "height": float(content["height"]),
                    "scale": 1,
                },
            },
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(base64.b64decode(shot["data"]))
        print(path)

    await with_page(state, action)


def command_cleanup() -> None:
    path = state_path()
    if not path.is_file():
        print("no instance")
        return
    state = json.loads(path.read_text())
    stop_pids(chrome_processes(state))
    stop_pids(serve_processes(state))
    home = run_home()
    for relative in ("project", "library", "chrome"):
        target = home / relative
        if target.exists():
            shutil.rmtree(target)
    for name in ("state.json", "serve.log", "chrome.log"):
        target = home / name
        if target.is_file():
            target.unlink()
    print("stopped")
    print(f"evidence={evidence_root()}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="control-vcc")
    commands = parser.add_subparsers(dest="cmd", required=True)
    commands.add_parser("launch")
    commands.add_parser("doctor")
    commands.add_parser("cleanup")
    read = commands.add_parser("read")
    read.add_argument("path")
    browser = commands.add_parser("browser")
    actions = browser.add_subparsers(dest="bcmd", required=True)
    actions.add_parser("open")
    click = actions.add_parser("click")
    click.add_argument("--role", required=True)
    click.add_argument("--name", required=True)
    fill = actions.add_parser("fill")
    fill.add_argument("--role", required=True)
    fill.add_argument("--name", required=True)
    fill.add_argument("--value", required=True)
    text = actions.add_parser("text")
    text.add_argument("--id", required=True)
    visible = actions.add_parser("visible")
    visible.add_argument("--id", required=True)
    wait = actions.add_parser("wait")
    wait.add_argument("--id", required=True)
    wait.add_argument("--text")
    wait.add_argument("--any", nargs="+")
    wait.add_argument("--timeout", type=float, default=30)
    snapshot = actions.add_parser("snapshot")
    snapshot.add_argument("--aria", action="store_true", required=True)
    snapshot.add_argument("--path", required=True)
    screenshot = actions.add_parser("screenshot")
    screenshot.add_argument("--path", required=True)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if not Path("/proc/self").is_dir():
        raise SystemExit("control-vcc needs Linux /proc to own the processes it starts")
    if args.cmd == "launch":
        command_launch()
    elif args.cmd == "doctor":
        command_doctor()
    elif args.cmd == "cleanup":
        command_cleanup()
    elif args.cmd == "read":
        command_read(args.path)
    elif args.cmd == "browser":
        command_browser(args)


if __name__ == "__main__":
    main()
