import fs from "node:fs";
import os from "node:os";
import { spawnSync } from "node:child_process";
const { outside } = JSON.parse(fs.readFileSync("/project/probe.json", "utf8"));
let readOnly = false;
try { fs.writeFileSync("/project/forbidden", "blocked"); } catch { readOnly = true; }
const limits = fs.readFileSync("/proc/self/limits", "utf8");
const status = fs.readFileSync("/proc/self/status", "utf8");
const limit = (name, maximum) => {
  const line = limits.split("\n").find((row) => row.startsWith(name));
  const values = line?.slice(name.length).trim().split(/\s+/).slice(0, 2);
  return values?.length === 2 && values.every((value) => /^\d+$/.test(value) && Number(value) <= maximum);
};
const nested = spawnSync("/usr/bin/unshare", ["--user", "/usr/bin/true"]);
const result = {
  no_network: Object.entries(os.networkInterfaces()).every(([name, addresses]) => name === "lo" && addresses.every((a) => a.internal)),
  no_host_secrets: Object.keys(process.env).every((k) => ["PATH", "NODE_PATH", "PWD"].includes(k)) && !fs.existsSync("/app") && !fs.existsSync("/run/secrets"),
  no_cross_project_files: !fs.existsSync(outside) && !fs.existsSync("/workspace"),
  read_only_sources: readOnly,
  resource_limits: limit("Max cpu time", 30) && limit("Max address space", 3000000000) && limit("Max processes", 64) && limit("Max open files", 128) && /NoNewPrivs:\s+1/.test(status) && /CapEff:\s+0+\n/.test(status),
  no_nested_userns: !nested.error && nested.status !== 0,
};
process.stdout.write(JSON.stringify(result));
