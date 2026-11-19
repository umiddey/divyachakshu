// omp tool_call gate for oi-agent. It runs the Python decider and blocks the
// call unless the decider returns a clean verdict (fail closed).
import { spawnSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { isAbsolute } from "node:path";
import type { ExtensionAPI } from "@oh-my-pi/pi-coding-agent";

const FAIL = { block: true, reason: "blocked by oi policy: check failed" };

function gate(toolName: unknown, input: unknown) {
	const policyPath = process.env.OI_OMP_POLICY;
	if (!policyPath) return FAIL;
	let policy: { python?: unknown; timeout_ms?: unknown };
	try {
		policy = JSON.parse(readFileSync(policyPath, "utf8"));
	} catch {
		return FAIL;
	}
	if (
		typeof policy.python !== "string" ||
		!isAbsolute(policy.python) ||
		typeof policy.timeout_ms !== "number"
	) {
		return FAIL;
	}
	const run = spawnSync(policy.python, ["-I", "-m", "oi_agent.opencode.omp_policy"], {
		input: JSON.stringify({ toolName, input }),
		env: { OI_OMP_POLICY: policyPath },
		encoding: "utf8",
		timeout: policy.timeout_ms,
		maxBuffer: 64 * 1024,
	});
	if (run.error || run.status !== 0 || run.signal) return FAIL;
	let verdict: unknown;
	try {
		verdict = JSON.parse(run.stdout);
	} catch {
		return FAIL;
	}
	if (verdict === null || typeof verdict !== "object") return FAIL;
	const { block, reason } = verdict as { block?: unknown; reason?: unknown };
	if (block === false) return undefined;
	if (block === true && typeof reason === "string") return { block: true, reason };
	return FAIL;
}

export default function oiPolicy(pi: ExtensionAPI): void {
	pi.on("tool_call", (event) => gate(event.toolName, event.input));
}
