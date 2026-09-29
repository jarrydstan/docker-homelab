#!/usr/bin/env python3
"""CI helper for the homelab compose stack (stdlib only).

  render            render the full stack with throwaway env/secrets (validates it)
  changed-images    images added by this branch compared to --base
  smoke             start changed services in an isolated project and wait for health

Everything is generated under a scratch root (default: .ci-root/), and compose
is always run with --env-file pointing there, so the real .env, secrets and
appdata on the host are never read or touched.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MASTER = "docker-compose-mstr.yaml"
SECRETS = ["cf_dns_api_tokens", "pgsql_root_password", "basic_auth_credentials", "cloudflare"]
VAR_RE = re.compile(r"(?<!\$)\$\{?([A-Za-z_][A-Za-z0-9_]*)(:?[-?])?")
IMAGE_RE = re.compile(r"^\+\s*image:\s*[\"']?([^\"'\s#]+)", re.M)


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def run(cmd, **kw):
    kw.setdefault("check", True)
    kw.setdefault("text", True)
    return subprocess.run(cmd, **kw)


def compose_files(repo):
    """The master file plus every compose file it includes (for the-fellowship)."""
    master = open(os.path.join(repo, MASTER)).read()
    inc = re.findall(r"^\s+-\s+compose/\$HOSTNAME/(\S+)", master, re.M)
    return [MASTER] + [f"compose/the-fellowship/{f}" for f in inc]


def prepare(repo, root):
    """Create env file, dummy secrets and env_files for rendering `repo`."""
    os.makedirs(os.path.join(root, ".secrets"), exist_ok=True)
    for d in ("appdata", "data"):
        os.makedirs(os.path.join(root, d), exist_ok=True)
    for s in SECRETS:
        with open(os.path.join(root, ".secrets", s), "w") as f:
            f.write("ci-placeholder\n")
    ci_dir = os.path.join(REPO, "ci")
    shutil.copy(os.path.join(ci_dir, "kaneo.ci.env"), os.path.join(root, ".kaneo.env"))
    shutil.copy(os.path.join(ci_dir, "rustfs.ci.env"), os.path.join(root, ".rustfs.env"))

    env = {}
    for line in open(os.path.join(ci_dir, "ci.env")):
        line = line.strip()
        if line and not line.startswith("#"):
            k, _, v = line.partition("=")
            env[k] = v.replace("@ROOT@", root)
    # Any other referenced variable gets a placeholder, unless it has a default.
    for f in compose_files(repo):
        for name, op in VAR_RE.findall(open(os.path.join(repo, f)).read()):
            if name not in env and not op:
                env[name] = "ci-placeholder"
    with open(os.path.join(root, "ci.env"), "w") as f:
        f.writelines(f"{k}={v}\n" for k, v in env.items())
    # dynacat reads $DOCKERDIR/.env as an env_file
    shutil.copy(os.path.join(root, "ci.env"), os.path.join(root, ".env"))
    return os.path.join(root, "ci.env")


def compose_env():
    """Minimal process env: shell vars (e.g. HOSTNAME) override --env-file."""
    keep = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME") or k.startswith("DOCKER_")}
    keep["HOSTNAME"] = "the-fellowship"
    return keep


def render(repo, root):
    envfile = prepare(repo, root)
    out = run(["docker", "compose", "--env-file", envfile, "-f", os.path.join(repo, MASTER),
               "config", "--format", "json"], capture_output=True, env=compose_env(), check=False)
    if out.returncode:
        log(out.stderr)
        sys.exit(f"compose config failed for {repo}")
    return json.loads(out.stdout)


def base_worktree(base):
    path = tempfile.mkdtemp(prefix="ci-base-")
    run(["git", "-C", REPO, "worktree", "add", "--detach", path, base], capture_output=True)
    return path


def drop_worktree(path):
    run(["git", "-C", REPO, "worktree", "remove", "--force", path], check=False, capture_output=True)


def changed_services(base, root):
    head = render(REPO, root)["services"]
    wt = base_worktree(base)
    try:
        old = render(wt, root + "-base")["services"]
    finally:
        drop_worktree(wt)
    # Paths differ between the two renders only by root; normalise before comparing.
    norm = lambda s: json.dumps(s, sort_keys=True).replace(root + "-base", root)
    return sorted(n for n, s in head.items() if n not in old or norm(s) != norm(old[n])), head


def cmd_render(a):
    cfg = render(REPO, a.root)
    log(f"rendered OK: {len(cfg['services'])} services")
    if a.output:
        json.dump(cfg, open(a.output, "w"), indent=1)


def cmd_changed_images(a):
    files = run(["git", "-C", REPO, "diff", "--name-only", f"{a.base}...HEAD", "--", "*.yaml", "*.yml"],
                capture_output=True).stdout.split()
    imgs = set()
    for f in files:
        diff = run(["git", "-C", REPO, "diff", f"{a.base}...HEAD", "--", f], capture_output=True).stdout
        imgs.update(IMAGE_RE.findall(diff))
    print("\n".join(sorted(imgs)))


def load_skip():
    skip = set()
    for line in open(os.path.join(REPO, "ci", "smoke-skip.txt")):
        name = line.split("#", 1)[0].strip()
        if name:
            skip.add(name)
    return skip


def smoke_project(services, targets, root):
    """Build an isolated compose project for targets and their dependencies."""
    skip = load_skip()

    def why_skip(name, seen=()):
        s = services[name]
        if name in skip:
            return "listed in ci/smoke-skip.txt"
        if "build" in s:
            return "built from appdata"
        nm = s.get("network_mode", "")
        if nm == "host" or nm.startswith("service:"):
            return f"network_mode {nm}"
        for dep in s.get("depends_on", {}):
            if dep not in seen and why_skip(dep, seen + (name,)):
                return f"depends on skipped service {dep}"
        return None

    run_set, skipped = set(), {}
    def add(name):
        if name not in run_set:
            run_set.add(name)
            for dep in services[name].get("depends_on", {}):
                add(dep)
    for t in targets:
        reason = why_skip(t)
        if reason:
            skipped[t] = reason
        else:
            add(t)

    out = {}
    for name in run_set:
        s = json.loads(json.dumps(services[name]))
        alias = s.pop("container_name", None)
        for k in ("ports", "labels", "dns", "secrets", "extra_hosts", "networks", "logging"):
            s.pop(k, None)
        s["restart"] = "no"
        if s.get("network_mode") != "none":
            s["networks"] = {"smoke": {"aliases": [alias]} if alias else {}}
        vols = []
        for v in s.get("volumes", []):
            if v.get("type") == "bind":
                src = v.get("source", "")
                if src == "/var/run/docker.sock":
                    vols.append(v)
                elif src.startswith(root) and "." not in os.path.basename(src):
                    # empty anonymous volume: keeps the image's ownership, unlike a bind dir
                    vols.append({"type": "volume", "target": v["target"]})
                # other host paths and single-file mounts are dropped
            elif v.get("type") == "volume":
                vols.append({"type": "volume", "target": v["target"]})
        s["volumes"] = vols
        out[name] = s
    return {"name": "ci-smoke", "services": out, "networks": {"smoke": {}}}, skipped


def container_states(project):
    ids = run(["docker", "ps", "-aq", "--filter", f"label=com.docker.compose.project={project}"],
              capture_output=True).stdout.split()
    if not ids:
        return {}
    info = json.loads(run(["docker", "inspect"] + ids, capture_output=True).stdout)
    return {c["Config"]["Labels"]["com.docker.compose.service"]: c["State"] for c in info}


def cmd_smoke(a):
    if a.services:
        targets, services = a.services, render(REPO, a.root)["services"]
    else:
        targets, services = changed_services(a.base, a.root)
    if not targets:
        log("no service definitions changed; nothing to smoke-test")
        return
    proj, skipped = smoke_project(services, targets, a.root)
    for t, why in skipped.items():
        log(f"SKIP {t}: {why}")
    if not proj["services"]:
        log("all changed services are skipped; validate/images jobs still gate this change")
        return
    project = f"ci-smoke-{os.getpid()}"
    proj["name"] = project
    pf = os.path.join(a.root, "smoke-compose.json")
    json.dump(proj, open(pf, "w"), indent=1)
    names = sorted(proj["services"])
    log(f"smoke-testing: {', '.join(names)}")
    dc = ["docker", "compose", "-p", project, "-f", pf]
    ok = False
    try:
        if run(dc + ["up", "-d", "--quiet-pull"], env=compose_env(), check=False).returncode:
            sys.exit("FAILED: docker compose up (see error above: missing tag, bad config, ...)")
        deadline = time.time() + a.timeout
        while time.time() < deadline:
            st = container_states(project)
            pending, bad = [], []
            for n in names:
                s = st.get(n)
                if not s:
                    pending.append(n)
                elif s.get("Health"):
                    h = s["Health"]["Status"]
                    (bad if h == "unhealthy" else pending if h != "healthy" else []).append(n)
                elif s["Status"] != "running":
                    bad.append(n)
            if bad:
                log(f"FAILED: {', '.join(bad)}")
                break
            if not pending:
                ok = True
                break
            time.sleep(5)
        else:
            log(f"TIMEOUT after {a.timeout}s waiting for: {', '.join(pending)}")
        for n, s in sorted(container_states(project).items()):
            h = s.get("Health", {}).get("Status", "-")
            log(f"  {n:24} {s['Status']:10} health={h}")
        if not ok:
            for n in names:
                log(f"----- logs: {n} -----")
                run(dc + ["logs", "--tail", "60", n], check=False, env=compose_env())
    finally:
        run(dc + ["down", "-v", "--remove-orphans", "-t", "5"], check=False, env=compose_env(),
            capture_output=True)
    if not ok:
        sys.exit(1)
    log("smoke test passed")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--root", default=os.path.join(REPO, ".ci-root"))
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("render"); r.add_argument("-o", "--output")
    c = sub.add_parser("changed-images"); c.add_argument("--base", required=True)
    s = sub.add_parser("smoke")
    s.add_argument("--base", default="origin/master")
    s.add_argument("--timeout", type=int, default=420)
    s.add_argument("services", nargs="*", help="test these services instead of the changed ones")
    a = p.parse_args()
    a.root = os.path.abspath(a.root)
    {"render": cmd_render, "changed-images": cmd_changed_images, "smoke": cmd_smoke}[a.cmd](a)


if __name__ == "__main__":
    main()
