import { resolve } from "node:path";
import { inspectSource } from "./probe";
import { Source, validateSshHost } from "./source";

async function main(): Promise<void> {
  const args = process.argv.slice(2);
  const options: { home?: string; python?: string; ssh?: string; sessionName?: string } = {};
  let source: Source = { kind: "local" };
  for (let index = 0; index < args.length; index += 2) {
    const value = args[index + 1];
    if (!value) throw new Error("missing_option_value");
    if (args[index] === "--ssh") source = { kind: "ssh", host: validateSshHost(value) };
    else if (args[index] === "--home") options.home = value;
    else if (args[index] === "--python") options.python = value;
    else if (args[index] === "--ssh-executable") options.ssh = value;
    else if (args[index] === "--session-name") options.sessionName = value;
    else throw new Error("unknown_option");
  }
  const result = await inspectSource(resolve(__dirname, "../.."), source, options);
  // Probe JSON deliberately contains no session names, prompts or assistant text.
  console.log(JSON.stringify(result, null, 2));
}

void main().catch(error => { console.error(error instanceof Error ? error.message : "probe_failed"); process.exitCode = 1; });
